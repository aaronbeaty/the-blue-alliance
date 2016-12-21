import logging
import tba_config

from google.appengine.api import search
from google.appengine.ext import ndb

from consts.award_type import AwardType
from consts.event_type import EventType
from controllers.base_controller import CacheableHandler
from helpers.location_helper import LocationHelper
from models.event_team import EventTeam
from template_engine import jinja2_engine


SORT_ORDER = {
    AwardType.CHAIRMANS: 0,
    AwardType.ENGINEERING_INSPIRATION: 1,
    AwardType.WINNER: 2,
    AwardType.FINALIST: 3,
    AwardType.WOODIE_FLOWERS: 4,
}


class NearbyController(CacheableHandler):
    VALID_YEARS = list(reversed(range(1992, tba_config.MAX_YEAR + 1)))
    # VALID_AWARD_TYPES = [
    #     (AwardType.CHAIRMANS, 'Chairman\'s'),
    #     (AwardType.ENGINEERING_INSPIRATION, 'Engineering Inspiration'),
    #     (AwardType.WINNER, 'Event Winner'),
    #     (AwardType.FINALIST, 'Event Finalist'),
    #     (AwardType.WOODIE_FLOWERS, 'Woodie Flowers'),
    # ]
    VALID_AWARD_TYPES = [kv for kv in AwardType.GENERIC_NAMES.items()]

    VALID_AWARD_TYPES = sorted(
        VALID_AWARD_TYPES,
        key=lambda (event_type, name): SORT_ORDER.get(event_type, name))
    VALID_EVENT_TYPES = [
        (EventType.CMP_DIVISION, 'Championship Division'),
        (EventType.CMP_FINALS, 'Einstein Field'),
    ]
    DEFAULT_SEARCH_TYPE = 'teams'
    PAGE_SIZE = 20
    METERS_PER_MILE = 5280 * 12 * 2.54 / 100
    CACHE_VERSION = 1
    CACHE_KEY_FORMAT = "nearby_{}_{}_{}_{}_{}_{}"  # (year, award_type, event_type, location, search_type, page)

    def __init__(self, *args, **kw):
        super(NearbyController, self).__init__(*args, **kw)
        self._cache_expiration = 60 * 60 * 24

    def _get_params(self):
        year = self.request.get('year')
        if not year:
            year = 0
        year = int(year)

        award_types = self.request.get('award_type', allow_multiple=True)
        if award_types:
            # Sort to make caching more likely
            award_types = sorted([int(award_type) for award_type in award_types])
        else:
            award_types = []

        event_types = self.request.get('event_type', allow_multiple=True)
        if event_types:
            # Sort to make caching more likely
            event_types = sorted([int(event_type) for event_type in event_types])
        else:
            event_types = []

        location = self.request.get('location', '')
        search_type = self.request.get('search_type', self.DEFAULT_SEARCH_TYPE)
        if search_type != 'teams' and search_type != 'events':
            search_type = self.DEFAULT_SEARCH_TYPE
        page = int(self.request.get('page', 0))

        return year, award_types, event_types, location, search_type, page

    def get(self):
        year, award_types, event_types, location, search_type, page = self._get_params()
        self._partial_cache_key = self.CACHE_KEY_FORMAT.format(year, award_types, event_types, location, search_type, page)
        super(NearbyController, self).get()

    def _render(self):
        year, award_types, event_types, location, search_type, page = self._get_params()

        num_results = 0
        results = []
        distances = []

        sort_options_expressions = [
            search.SortExpression(
                expression='number',
                direction=search.SortExpression.ASCENDING
            )]
        returned_expressions = []

        query_string = 'year={}'.format(year)
        if location:
            lat_lon = LocationHelper.get_lat_lng(location)
            if lat_lon:
                lat, lon = lat_lon
                dist_expr = 'distance(location, geopoint({}, {}))'.format(lat, lon)
                query_string = '{} > {} AND year={}'.format(dist_expr, -1, year)

                sort_options_expressions = [
                    search.SortExpression(
                        expression=dist_expr,
                        direction=search.SortExpression.ASCENDING
                    )]

                returned_expressions = [
                    search.FieldExpression(
                        name='distance',
                        expression=dist_expr
                    )]

        returned_fields = ['bb_count']
        if award_types and event_types:
            for award_type in award_types:
                for event_type in event_types:
                    field = 'event_award_{}_{}_count'.format(event_type, award_type)
                    query_string += ' AND {} > 0'.format(field, event_type, award_type)
                    returned_fields += [field]
        elif award_types:
            for award_type in award_types:
                field = 'award_{}_count'.format(award_type)
                query_string += ' AND {} > 0'.format(field, award_type)
                returned_fields += [field]
        elif event_types:
            for event_type in event_types:
                field = 'event_{}_count'.format(event_type)
                query_string += ' AND {} > 0'.format(field, event_type)
                returned_fields += [field]

        query = search.Query(
            query_string=query_string,
            options=search.QueryOptions(
                limit=self.PAGE_SIZE,
                number_found_accuracy=10000,  # Larger than the number of possible results
                offset=self.PAGE_SIZE * page,
                returned_fields=returned_fields,
                sort_options=search.SortOptions(
                    expressions=sort_options_expressions
                ),
                returned_expressions=returned_expressions
            )
        )
        if search_type == 'teams':
            search_index = search.Index(name="teamYear")
        else:
            search_index = search.Index(name="eventLocation")

        docs = search_index.search(query)
        num_results = docs.number_found
        distances = {}
        keys = []
        all_fields = []
        for result in docs.results:
            key = result.doc_id.split('_')[0]

            # Save fields
            fields = {}
            for field in result.fields:
                fields[field.name] = field.value
            all_fields.append(fields)

            # Save distances
            if location and lat_lon:
                distances[key] = result.expressions[0].value / self.METERS_PER_MILE
            if search_type == 'teams':
                keys.append(ndb.Key('Team', key))
            else:
                keys.append(ndb.Key('Event', key))

        result_futures = ndb.get_multi_async(keys)

        # Construct field names
        field_names = []
        for field in returned_fields:
            if field == 'bb_count':
                field_names.append('Blue Banners')
            else:
                field_names.append(field)

        results = zip([result_future.get_result() for result_future in result_futures], all_fields)

        self.template_values.update({
            'valid_years': self.VALID_YEARS,
            'valid_award_types': self.VALID_AWARD_TYPES,
            'num_special_awards': len(SORT_ORDER),
            'valid_event_types': self.VALID_EVENT_TYPES,
            'page_size': self.PAGE_SIZE,
            'page': page,
            'year': year,
            'award_types': award_types,
            'event_types': event_types,
            'location': location,
            'search_type': search_type,
            'num_results': num_results,
            'results': results,
            'returned_fields': returned_fields,
            'field_names': field_names,
            # 'bb_count': bb_count,
            'distances': distances,
        })

        return jinja2_engine.render('advanced_search.html', self.template_values)
