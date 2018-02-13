from django.conf import settings
from django.contrib import messages
from django.http import Http404, JsonResponse, QueryDict, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.template import Context, RequestContext, loader, TemplateDoesNotExist
from django.utils import timezone
from django.utils.encoding import force_text
from django.utils.html import escape
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from django.views.generic import View
from elasticsearch_dsl.utils import AttrList
from elasticsearch_dsl import Q
import elasticsearch_dsl as dsl
import six

from seeker.templatetags.seeker import seeker_format

from .mapping import DEFAULT_ANALYZER

import collections
import inspect
import re
import json
from django.http.response import HttpResponseBadRequest, HttpResponseForbidden

class Column (object):
    """
    """

    view = None
    visible = False

    def __init__(self, field, label=None, sort=None, value_format=None, template=None, header=None, export=True, highlight=None):
        self.field = field
        self.label = label if label is not None else field.replace('_', ' ').replace('.raw', '').capitalize()
        self.sort = sort
        self.template = template
        self.value_format = value_format
        self.header_html = escape(self.label) if header is None else header
        self.export = export
        self.highlight = highlight

    def __str__(self):
        return self.label

    def __repr__(self):
        return 'Column(%s)' % self.field

    def bind(self, view, visible):
        self.view = view
        self.visible = visible
        if self.visible:
            self.template_obj = self.view.get_field_template(self.field)
            if self.template:
                try:
                    self.template_obj = loader.get_template(self.template)
                except TemplateDoesNotExist:
                    pass
        return self

    def header(self):
        cls = '%s_%s' % (self.view.document._doc_type.name, self.field.replace('.', '_'))
        if not self.sort:
            return mark_safe('<th class="%s">%s</th>' % (cls, self.header_html))
        q = self.view.request.GET.copy()
        field = q.get('s', '')
        sort = None
        cls += ' sort'
        if field.lstrip('-') == self.field:
            # If the current sort field is this field, give it a class a change direction.
            sort = 'Descending' if field.startswith('-') else 'Ascending'
            cls += ' desc' if field.startswith('-') else ' asc'
            d = '' if field.startswith('-') else '-'
            q['s'] = '%s%s' % (d, self.field)
        else:
            q['s'] = self.field
        next_sort = 'descending' if sort == 'Ascending' else 'ascending'
        sr_label = (' <span class="sr-only">(%s)</span>' % sort) if sort else ''
        html = '<th class="%s"><a href="?%s" title="Click to sort %s" data-sort="%s">%s%s</a></th>' % (cls, q.urlencode(), next_sort, q['s'], self.header_html, sr_label)
        return mark_safe(html)

    def context(self, result, **kwargs):
        return kwargs

    def render(self, result, **kwargs):
        value = getattr(result, self.field, None)
        if self.value_format:
            value = self.value_format(value)
        try:
            if '*' in self.highlight:
                # If highlighting was requested for multiple fields, grab any matching fields as a dictionary.
                r = self.highlight.replace('*', r'\w+').replace('.', r'\.')
                highlight = {f: result.meta.highlight[f] for f in result.meta.highlight if re.match(r, f)}
            else:
                highlight = result.meta.highlight[self.highlight]
        except:
            highlight = []
        params = {
            'result': result,
            'field': self.field,
            'value': value,
            'highlight': highlight,
            'view': self.view,
            'user': self.view.request.user,
            'query': self.view.get_keywords(request.GET),
        }
        params.update(self.context(result, **kwargs))
        return self.template_obj.render(params)

    def export_value(self, result):
        export_field = self.field if self.export is True else self.export
        if export_field:
            value = getattr(result, export_field, '')
            export_val = ', '.join(force_text(v.to_dict() if hasattr(v, 'to_dict') else v) for v in value) if isinstance(value, AttrList) else seeker_format(value)
        else:
            export_val = ''
        return export_val

class SeekerView (View):
    is_advanced_search = False
    """
    Flag to indicate if this SeekerView is an advanced search which allows complex boolean queries.
    """
    
    boolean_translations = {
        'AND': 'must',
        'OR': 'should'
    }
    """
    This dictionary translates the boolean operators passed from the frontend into their elasticsearch equivalents.
    """
    
    can_save = True
    """
    Whether searches for this view can be saved.
    """
    
    columns = None
    """
    A list of Column objects, or strings representing mapping field names. If None, all mapping fields will be available.
    """

    display = None
    """
    A list of field/column names to display by default.
    """
    
    document = None
    """
    A :class:`elasticsearch_dsl.DocType` class to present a view for.
    """

    exclude = None
    """
    A list of field names to exclude when generating columns.
    """

    export_name = 'seeker'
    """
    The filename (without extension, which will be .csv) to use when exporting data from this view.
    """

    export_timestamp = False
    """
    Whether or not to append a timestamp of the current time to the export filename when exporting data from this view.
    """

    extra_context = {}
    """
    Extra context variables to use when rendering. May be passed via as_view(), or overridden as a property.
    """
    
    facets = []
    """
    A list of :class:`seeker.Facet` objects that are available to facet the results by.
    """

    field_columns = {}
    """
    A dictionary of field column overrides.
    """

    field_labels = {}
    """
    A dictionary of field label overrides.
    """
    
    field_templates = {}
    """
    A dictionary of field template overrides.
    """

    _field_templates = {}
    """
    A dictionary of default templates for each field
    """
    
    footer_template = 'seeker/footer.html'
    """
    The template used to render the search results footer.
    """

    header_template = 'seeker/header.html'
    """
    The template used to render the search results header.
    """

    highlight = True
    """
    A list of field names to highlight, or True/False to enable/disable highlighting for all fields.
    """

    highlight_encoder = 'html'
    """
    An 'encoder' parameter is used when highlighting to define how highlighted text will be encoded. It can be either
    'default' (no encoding) or 'html' (will escape html, if you use html highlighting tags).
    """

    highlight_fields = {}
    """
    A dictionary of highlight field overrides.
    """

    index = None
    """
    The ES index to use. Defaults to the SEEKER_INDEX setting.
    """

    initial_facets = {}
    """
    A dictionary of initial facets, mapping fields to lists of initial values.
    """

    operator = getattr(settings, 'SEEKER_DEFAULT_OPERATOR', 'AND')
    """
    The query operator to use by default.
    """

    page_size = 10
    """
    The number of results to show per page.
    """

    page_spread = 7
    """
    The number of pages (not including first and last) to show in the paginator widget.
    """

    permission = None
    """
    If specified, a permission to check (using ``request.user.has_perm``) for this view.
    """

    query_type = getattr(settings, 'SEEKER_QUERY_TYPE', 'query_string')
    """
    The query type to use when performing keyword queries (either 'query_string' (default) or 'simple_query_string').
    """

    required_display = []
    """
    A list of tuples, ex. ('field name', 0), representing field/column names that will always be displayed (cannot be hidden by the user).
    The second value is the index/position of the field (used as the index in list.insert(index, 'field name')).
    """
    @property
    def required_display_fields(self):
        return [t[0] for t in self.required_display]

    results_template = 'seeker/results.html'
    """
    The template used to render the search results.
    """

    search = None
    """
    A list of field names to search. By default, will included all fields defined on the document mapping.
    """

    show_rank = True
    """
    Whether or not to show a Rank column when performing keyword searches.
    """
    
    sort = None
    """
    A list of field/column names to sort by default, or None for no default sort order.
    """

    sort_fields = {}
    """
    A dictionary of sort field overrides.
    """
    
    template_name = 'seeker/seeker.html'
    """
    The overall seeker template to render.
    """

    using = None
    """
    The ES connection alias to use.
    """

    def normalized_querystring(self, qs=None, ignore=None):
        """
        Returns a querystring with empty keys removed, keys in sorted order, and values (for keys whose order does not
        matter) in sorted order. Suitable for saving and comparing searches.

        :param qs: (Optional) querystring to use; defaults to request.GET
        :param ignore: (Optional) list of keys to ignore when building the querystring
        """
        data = QueryDict(qs) if qs is not None else self.request.GET
        parts = []
        for key in sorted(data):
            if ignore and key in ignore:
                continue
            if not data[key]:
                continue
            if key == 'p' and data[key] == '1':
                continue
            values = data.getlist(key)
            # Make sure display/facet/sort fields maintain their order. Everything else can be sorted alphabetically for consistency.
            if key not in ('d', 'f', 's'):
                values = sorted(values)
            parts.extend(urlencode({key: val}) for val in values)
        return '&'.join(parts)

    def get_field_label(self, field_name):
        """
        Given a field name, returns a human readable label for the field.
        """
        if field_name.endswith('.raw'):
            field_name = field_name[:-4]
        if field_name in self.field_labels:
            return self.field_labels[field_name]
        try:
            # If the document is a ModelIndex, try to get the verbose_name of the Django field.
            f = self.document.queryset().model._meta.get_field(field_name)
            return f.verbose_name[0].upper() + f.verbose_name[1:]
        except:
            # Otherwise, just make the field name more human-readable.
            return field_name.replace('_', ' ').capitalize()

    def get_field_sort(self, field_name):
        """
        Given a field name, returns the field name that should be used for sorting. If a mapping defines
        a .raw sub-field, that is used, otherwise the field name itself is used if index=not_analyzed.
        """
        if field_name.endswith('.raw'):
            return field_name
        if field_name in self.sort_fields:
            return self.sort_fields[field_name]
        if field_name in self.document._doc_type.mapping:
            dsl_field = self.document._doc_type.mapping[field_name]
            if isinstance(dsl_field, (dsl.Object, dsl.Nested)):
                return None
            if not isinstance(dsl_field, dsl.String):
                return field_name
            if 'raw' in dsl_field.fields:
                return '%s.raw' % field_name
            elif getattr(dsl_field, 'index', None) == 'not_analyzed':
                return field_name
        return None

    def get_field_template(self, field_name):
        """
        Returns the default template instance for the given field name.
        """
        try:
            return self._field_templates[field_name]
        except KeyError:
            return self.find_field_template(field_name)

    @classmethod
    def find_field_template(cls, field_name):
        """
        finds and sets the default template instance for the given field name with the given template.
        """
        search_templates = []
        if field_name in cls.field_templates:
            search_templates.append(cls.field_templates[field_name])
        for _cls in inspect.getmro(cls.document):
            if issubclass(_cls, dsl.DocType):
                search_templates.append('seeker/%s/%s.html' % (_cls._doc_type.name, field_name))
        search_templates.append('seeker/column.html')
        template = loader.select_template(search_templates)
        existing_templates = list(set(cls._field_templates.values()))
        for existing_template in existing_templates:
            #If the template object already exists just re-use the existing one.
            if template.template.name == existing_template.template.name:
                template = existing_template
                break
        cls._field_templates.update({field_name: template})
        return template

    @classmethod
    def update_field_template(cls, field_name, template):
        """
        Updates the _field_template instance of field_name with template object for the entire class
        """
        cls._field_templates.update({field_name: template})

    def get_field_highlight(self, field_name):
        if field_name in self.highlight_fields:
            return self.highlight_fields[field_name]
        if field_name in self.document._doc_type.mapping:
            dsl_field = self.document._doc_type.mapping[field_name]
            if isinstance(dsl_field, (dsl.Object, dsl.Nested)):
                return '%s.*' % field_name
            return field_name
        return None

    def make_column(self, field_name):
        """
        Creates a :class:`seeker.Column` instance for the given field name.
        """
        if field_name in self.field_columns:
            return self.field_columns[field_name]
        label = self.get_field_label(field_name)
        sort = self.get_field_sort(field_name)
        highlight = self.get_field_highlight(field_name)
        return Column(field_name, label=label, sort=sort, highlight=highlight)

    def get_columns(self, display):
        """
        Returns a list of :class:`seeker.Column` objects based on self.columns, converting any strings.
        """
        columns = []
        if not self.columns:
            # If not specified, all mapping fields will be available.
            for f in self.document._doc_type.mapping:
                if self.exclude and f in self.exclude:
                    continue
                columns.append(self.make_column(f))
        else:
            # Otherwise, go through and convert any strings to Columns.
            for c in self.columns:
                if isinstance(c, six.string_types):
                    if self.exclude and c in self.exclude:
                        continue
                    columns.append(self.make_column(c))
                elif isinstance(c, Column):
                    if self.exclude and c.field in self.exclude:
                        continue
                    columns.append(c)
        # Make sure the columns are bound and ordered based on the display fields (selected or default).
        visible_columns = []
        non_visible_columns=[]
        for c in columns:
            c.bind(self, c.field in display)
            if c.visible:
                visible_columns.append(c)
            else:
                non_visible_columns.append(c)
        visible_columns.sort(key=lambda  c: display.index(c.field))
        non_visible_columns.sort(key=lambda c: c.label)
        
        return visible_columns + non_visible_columns

    def get_keywords(self, data_dict):
        return data_dict.get('q', '').strip()

    def get_facets(self):
        return list(self.facets) if self.facets else []

    def get_display(self, data_dict):
        """
        Returns a list of display field names. If the user has selected display fields, those are used, otherwise
        the default list is returned. If no default list is specified, all fields are displayed.
        """
        default = list(self.display) if self.display else list(self.document._doc_type.mapping)
        display_fields = [f for f in data_dict.get('d', default) if f not in self.required_display_fields]
        for field, i in self.required_display:
            display_fields.insert(i, field)
        return display_fields

    def get_saved_search(self):
        """
        Returns the "saved_search" GET parameter if it's in the proper format, otherwise returns None.
        """
        saved_search_vals = [val for val in self.request.GET.getlist('saved_search') if val]
        if len(saved_search_vals) == 1 and saved_search_vals[0].isdigit():
            return saved_search_vals[0]
        return None

    def get_facet_data(self, initial=None, exclude=None):
        if initial is None:
            initial = {}
        facets = collections.OrderedDict()
        for f in self.get_facets():
            if f.field != exclude:
                facets[f] = self.request.GET.getlist(f.field) or initial.get(f.field, [])
        return facets

    def get_search_fields(self, mapping=None, prefix=''):
        if self.search:
            return self.search
        elif mapping is not None:
            fields = []
            for field_name in mapping:
                if mapping[field_name].to_dict().get('analyzer') == DEFAULT_ANALYZER:
                    fields.append(prefix + field_name)
                if hasattr(mapping[field_name], 'properties'):
                    fields.extend(self.get_search_fields(mapping=mapping[field_name].properties, prefix=prefix + field_name + '.'))
            return fields
        else:
            return self.get_search_fields(mapping=self.document._doc_type.mapping)

    def get_search_query_type(self, search, keywords, analyzer=DEFAULT_ANALYZER):
        kwargs = {'query': keywords,
                  'analyzer': analyzer,
                  'fields': self.get_search_fields(),
                  'default_operator': self.operator}
        if self.query_type == 'simple_query':
            kwargs['auto_generate_phrase_queries'] = True
        return search.query(self.query_type, **kwargs)

    def get_search(self, keywords=None, facets=None, aggregate=True):
        s = self.get_search_object()
        if keywords:
            s = self.get_search_query_type(s, keywords)
        if facets:
            for facet, values in facets.items():
                if values:
                    s = facet.filter(s, values)
                if aggregate:
                    facet.apply(s)
        return s
    
    def get_search_object(self):
        using = self.using or self.document._doc_type.using or 'default'
        index = self.index or self.document._doc_type.index or getattr(settings, 'SEEKER_INDEX', 'seeker')
        # TODO: self.document.search(using=using, index=index) once new version is released
        return self.document.search().index(index).using(using).extra(track_scores=True)

    def render(self, keywords, search, columns, sorts, page, facets=None, selected_facets=None, saved_search=None, saved_searches=None):
        # Make sure we sanitize the sort fields.
        sort_fields = []
        column_lookup = {c.field: c for c in columns}
        if not sorts:
            if keywords:
                sorts = []
            else:
                sorts = self.sort or []
        for s in sorts:
            # Get the column based on the field name, and use it's "sort" field, if applicable.
            c = column_lookup.get(s.lstrip('-'))
            if c and c.sort:
                sort_fields.append('-%s' % c.sort if s.startswith('-') else c.sort)

        # Highlight fields.
        if self.highlight:
            highlight_fields = self.highlight if isinstance(self.highlight, (list, tuple)) else [c.highlight for c in columns if c.visible and c.highlight]
            search = search.highlight(*highlight_fields, number_of_fragments=0).highlight_options(encoder=self.highlight_encoder)

        # Calculate paging information.
        offset = (page - 1) * self.page_size
        results_count = search[0:0].execute().hits.total
        if results_count < offset:
            page = 1
            offset = 0

        # Finally, grab the results.
        results = search.sort(*sort_fields)[offset:offset + self.page_size].execute()

        context_querystring = self.normalized_querystring(ignore=['p'])
        sort = sorts[0] if sorts else None
        context = {
            'is_advanced_search': self.is_advanced_search,
            'document': self.document,
            'can_save': self.can_save and self.request.user and self.request.user.is_authenticated(),
            'columns': columns,
            'display_columns': [c for c in columns if c.visible],
            'export_name': self.export_name,
            'facets': facets,
            'footer_template': self.footer_template,
            'form_action': self.request.path,
            'header_template': self.header_template,
            'keywords': keywords,
            'optional_columns': [c for c in columns if c.field not in self.required_display_fields],
            'page': page,
            'page_spread': self.page_spread,
            'page_size': self.page_size,
            'querystring': context_querystring,
            'reset_querystring': self.normalized_querystring(ignore=['p', 's', 'saved_search']),
            'results': results,
            'results_template': self.results_template,
            'saved_search': saved_search,
            'saved_searches': saved_searches,
            'selected_facets': selected_facets,
            'show_rank': self.show_rank,
            'sort': sort,
        }

        if self.extra_context:
            context.update(self.extra_context)

        if self.request.is_ajax():
            return JsonResponse({
                'facet_data': {facet.field: facet.data(results) for facet in self.get_facets()},
                'page': page,
                'querystring': context_querystring,
                'sort': sort,
                'saved_search_pk': saved_search.pk if saved_search else '',
                'table_html': loader.render_to_string(self.results_template, context, request=self.request),
            })
        else:
            return self.render_to_response(context)
        
    def render_to_response(self, context):
        return render(self.request, self.template_name, context)

    def get(self, request, *args, **kwargs):
        saved_search_id = kwargs.get('saved_search_id', None)
        # Check if a saved search id is passed in via the URL
        if saved_search_id:
            filters = { 'url': request.path, 'pk': saved_search_id }
            if self.restrict_to_user:
                filters['user'] = request.user
            try:
                saved_search = SavedSearch.objects.get(**filters)
            except SavedSearch.DoesNotExist:
                return HttpResponseBadRequest("Saved search could not be found.")
            return self.load_saved_search(saved_search)
        # Check if parameters are passed in (indicating a stateless simple search)
        elif len(request.GET):
            return self.simple_search()
        # Check for a default search
        default_search = get_default_search()
        if default_search:
            return self.load_saved_search(default_search)
        # Fall back to loading all results (no filters applied)
        self.all_results_search()
        
    def get_default_search(self, request):
        filters = { 'url': request.path, 'default': True }
        if self.restrict_to_user:
            filters['user'] = request.user
        return SavedSearch.object.filter(**filters).first()
        
    def load_saved_search(self, saved_search):
        data = json.load(saved_search.data)
        return self.advanced_search(data)
    
    def all_results_search(self):
        # TODO - Build way to get all results cleanly
        pass
    
    def simple_search(self):
        """
        This function performs a search based on the GET query string parameters.
        """
        if '_facet' in request.GET:
            return self.render_facet_query()
        else:
            sorts = self.request.GET.getlist('s', None)
            page = self.request.GET.get('p', '').strip()
            page = int(page) if page.isdigit() else 1
            keywords = self.get_keywords(self.request.GET)
            facets = self.get_facet_data(initial=self.initial_facets if not self.request.is_ajax() else None)
            selected_facets = self.request.GET.getlist('f') or self.initial_facets.keys()
            search = self.get_search(keywords, facets)
            display = self.get_display(self.request.GET)
            columns = self.get_columns(display)
            
            if '_export' in request.GET:
                return self.export(request, keywords, facets, search, display, columns)
            
            return self.render(keywords, search, columns, sorts, page, facets, selected_facets)
        
    def render_facet_query(self):
        keywords = self.get_keywords(self.request.GET)
        facet = {f.field: f for f in self.get_facets()}.get(self.request.GET.get('_facet'))
        if not facet:
            raise Http404()
        # We want to apply all the other facet filters besides the one we're querying.
        facets = self.get_facet_data(exclude=facet)
        search = self.get_search(keywords, facets, aggregate=False)
        fq = '.*' + self.request.GET.get('_query', '').strip() + '.*'
        facet.apply(search, include={'pattern': fq, 'flags': 'CASE_INSENSITIVE'})
        return JsonResponse(facet.data(search.execute()))

    def post(self, request, *args, **kwargs):
        saved_search_id = kwargs.get('saved_search_id', None)
        if saved_search_id:
            # Saved searches are separated by the root URL of the seeker instance so we strip off the saved_search_id parameter
            root_url = '/'.join(request.path.split('/')[:-1]) + '/'
            filters = { 'url': root_url, 'pk': saved_search_id }
            if self.restrict_to_user:
                filters['user'] = request.user
            try:
                saved_search = SavedSearch.object.get(**filters)
            except SavedSearch.DoesNotExist:
                return HttpResponseBadRequest('Saved search could not be found.')
            
            if request.POST.get('mark_default', False):
                saved_search.default = True
                SavedSearch.objects.filter(user=request.user, url=saved_search.url).update(default=False)
            elif request.POST.get('unmark_default', False):
                saved_search.default = False
                
            if request.POST.get('mark_saved', False):
                saved_search.saved = True
            elif request.POST.get('unmark_saved', False):
                saved_search.default = False
                saved_search.saved = False
                
            saved_search.save()
        else:
            name = request.POST.get('name', None).strip()
            if not name:
                return HttpResponseBadRequest('No name specified for save search.')
            saved_search = SavedSearch.objects.create(
                user = self.request.user, 
                data = json.dump(request.POST),
                name = name,
                url = request.path
            )
            
        return self.load_saved_search(saved_search)
        
    def advanced_search(self, data):
        """
        This function will process a complex query and return the results.
        The query_dict is a dictionary representation of the complex query. The following is an example of the accepted format:
        {
            "condition": "<boolean operator>",
            "rules": [
                {
                    "field": "<elasticsearch field name>",
                    "operator": "<comparison operator>",
                    "value": "<search value>"
                },
                {
                    "condition": "<boolean operator>",
                    "rules": [
                        {
                            "field": "<elasticsearch field name>",
                            "operator": "<comparison operator>",
                            "value": "<search value>"
                        }, ...
                    ],
                    "not": <flag to negate sibling rules>
                }, ...
            ],
            "not": <flag to negate sibling rules>
        }
        
        NOTES:
        Each 'rule' is a dictionary containing single rules and groups of rules. The value for each rule field are as follows:
            - field:     The name of the field in the elasticsearch document being searched.
            - operator:  A key in COMPARISON_CONVERSION dictionary. It is up to you to ensure the operator will work with the given field.
            - value:     The value to be used in the comparison for this rule
        Each group of rules will have:
            - condition: The boolean operator to apply to all rules in this group.
            - rules: A list of dictionaries containing either groups or rules.
            - not: A boolean (true/false) to indicate if this group should be additive or subtractive to the search.
        """
        query_dict = json.loads(data.get('query', ''))
        if not query_dict:
            return HttpResponseBadRequest(u'POST must include a JSON dictionary (representing the complex query) passed in via "query".')
        
        facets = { facet.field: facet for facet in self.get_facets() }
        complex_query = self.build_query(query_dict, facets)
        search = self.get_search_object().query(complex_query)
        
        sorts = data.get('s', None)
        page = data.get('p', '').strip()
        page = int(page) if page.isdigit() else 1
            
        keywords = self.get_keywords(data)
        display = self.get_display(data)
        columns = self.get_columns(display)
        
        if '_export' in data:
            return self.export(request, keywords, facets, search, display, columns)
        
        return self.render(keywords, search, columns, sorts, page, facets)
        
    def build_query(self, query_dict, facets):
        # Check if all required keys are present for an individual rule
        if all(k in query_dict for k in ('field', 'operator', 'value')):
            facet = facets.get(query_dict['field'])
            return facet.es_query(query_dict['operator'], query_dict['value'])
        
        # Check if all required keys are present for a group   
        elif all(k in query_dict for k in ('condition', 'rules')):
            group_operator = self.boolean_translations.get(query_dict.get('condition'), None)
            if not group_operator:
                raise ValueError(u"'{}' is not a valid boolean operator.".format(v))
            
            queries = []
            # The central portion of the recursion, we iterate over all rules inside this group
            for dict in query_dict.get('rules'):
                queries.append(self.build_query(dict, facets))
                
            if query_dict.get('not', False):
                return ~Q('bool', **{group_operator: queries})
            else:
                return Q('bool', **{group_operator: queries})
            
        # The query_dict must have been missing something, so we cannot create this query
        else:
            raise ValueError(u"The dictionary passed in did not have the proper structure. Dictionary: {}".format(str(query_dict)))

    def export(self, request, keywords, facets, search, display, columns):
        """
        A helper method called when ``_export`` is present in ``request.GET``. Returns a ``StreamingHttpResponse``
        that yields CSV data for all matching results.
        """
        def csv_escape(value):
            if isinstance(value, (list, tuple)):
                value = '; '.join(force_text(v) for v in value)
            return '"%s"' % force_text(value).replace('"', '""')

        def csv_generator():
            yield ','.join('"%s"' % c.label for c in columns if c.visible and c.export) + '\n'
            for result in search.scan():
                yield ','.join(csv_escape(c.export_value(result)) for c in columns if c.visible and c.export) + '\n'

        export_timestamp = ('_' + timezone.now().strftime('%m-%d-%Y_%H-%M-%S')) if self.export_timestamp else ''
        export_name = '%s%s.csv' % (self.export_name, export_timestamp)
        resp = StreamingHttpResponse(csv_generator(), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = 'attachment; filename=%s' % export_name
        return resp
    
    def check_permission(self, request):
        """
        Check to see if the user has permission for this view. This method may optionally return an ``HttpResponse``.
        """
        if self.permission and not request.user.has_perm(self.permission):
            raise Http404

    def dispatch(self, request, *args, **kwargs):
        """
        Overridden to perform permission checking by calling ``self.check_permission``.
        """
        resp = self.check_permission(request)
        if resp is not None:
            return resp
        return super(SeekerView, self).dispatch(request, *args, **kwargs)