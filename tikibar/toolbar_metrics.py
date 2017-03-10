from collections import defaultdict, namedtuple
import importlib
import inspect
import re
import time

from django.core.cache import cache

from .middleware import get_current_request
from .utils import (
    get_tiki_token_or_false,
    TIKIBAR_DATA_STORAGE_TIMEOUT,
    find_view_subpath,
    format_dict_as_lines,
)


MAX_SIZE_PER_CHUNK = 900 * 1024


Metric = namedtuple('Metric', (
    'timestamp', 'type', 'value', 'duration', 'props'
))


def publish_metrics_list(correlation_id, metrics_list):
    counter_key = 'tikibar-counter:%s' % correlation_id
    if cache.add(counter_key, '0', timeout=6000):
        counter = 0
    else:
        counter = cache.incr(counter_key)
    key = 'tikibar:%s:%d' % (correlation_id, counter)
    # TODO: write out chunks of the list up to MAX_SIZE_PER_CHUNK
    print 'publish_toolbar_metrics:'
    cache.set(key, metrics_list)
    print key, metrics_list


def publish_toolbar_metrics(correlation_id, metrics):
    cache_key = "tikibar:%s" % (correlation_id)
    cache.set(cache_key, metrics, TIKIBAR_DATA_STORAGE_TIMEOUT)


def get_toolbar():
    """Create and Return persistent MetricsContainer on the current request"""
    container = None
    request = get_current_request()
    if not request:
        return ToolbarMetricsContainer('no-correlation-id-because-no-request', False)

    if not hasattr(request, 'correlation_id'):
        return ToolbarMetricsContainer('no-correlation-id', False)

    # Toolbar active state is dictated by signed cookie
    is_active = bool(get_tiki_token_or_false(request))

    if request and not getattr(request, 'toolbar_metrics', None):
        container = ToolbarMetricsContainer(request.correlation_id, is_active)
        request.toolbar_metrics = container
    else:
        container = request.toolbar_metrics

    return container


class ToolbarMetricsContainer(object):

    # If the metrics are longer than this, they'll be dropped in an
    # attempt to fit into memcached
    max_size = 1000 * 1024

    def __init__(self, correlation_id, is_active=True):
        print "ToolbarMetricsContainer: correlation_id=%s" % correlation_id
        self.metrics_list = []
        self.flushed_time = time.time()
        self.metrics = defaultdict(list)
        self.metrics['queries'] = defaultdict(list)
        self.correlation_id = correlation_id
        self._is_active = is_active

    def is_active(self):
        return self._is_active

    def set_view_callable(self, view_func):
        module = view_func.__module__
        filepath = importlib.import_module(module).__file__
        if filepath.endswith('.pyc'):
            filepath = filepath[:-4] + '.py'
        try:
            view_func_s = view_func.__name__ + inspect.formatargspec(*inspect.getargspec(view_func))
        except Exception:
            view_func_s = str(view_func)

        self.add_singular_metric('view', view_func_s)
        self.add_singular_metric(
            'view_filepath',
            find_view_subpath(filepath),
        )

    def add_metric(self, metric):
        self.metrics_list.append(metric)
        # Force a write if it's been more than 1 second
        if (time.time() - self.flushed_time) > 1.0:
            publish_metrics_list(self.correlation_id, self.metrics_list)
            self.metrics_list = []
            self.flushed_time = time.time()

    def add_timed_metric(self, metric_type, val, start, stop):
        self.metrics[metric_type].append((val, {'d': (start, stop)}))
        self.add_metric(
            Metric(
                timestamp=start,
                type=metric_type,
                value=val,
                duration=stop - start,
                props=None
            )
        )

    def add_query_metric(self, metric_type, query_type, val, start, stop, needs_format=False):
        self.metrics['queries'][metric_type].append(
            (query_type, val, needs_format, {'d': (start, stop)})
        )
        self.add_metric(
            Metric(
                timestamp=start,
                type='query',
                value=val,
                duration=stop - start,
                props={
                    'metric_type': metric_type,
                    'query_type': query_type,
                    'needs_format': needs_format,
                }
            )
        )

    def add_sql_query_metric(self, query_type, val, start, stop):
        self.add_query_metric(
            metric_type='SQL',
            query_type=query_type,
            val=val,
            start=start,
            stop=stop,
            needs_format=True,
        )

    def add_freeform_metric(self, metric_type, data):
        self.metrics[metric_type].append(data)
        self.add_metric(
            Metric(
                timestamp=time.time(),
                type=metric_type,
                value=data,
                duration=None,
                props=None,
            )
        )

    def add_singular_metric(self, metric_type, data):
        self.metrics[metric_type] = data
        self.add_metric(
            Metric(
                timestamp=time.time(),
                type=metric_type,
                value=data,
                duration=None,
                props=None,
            )
        )

    def add_analytics_action_metric(self, data):
        """Add Analytics data to the toolbar.

        Parameters
        ----------

        - `data`: dict, should have an `actions` key whose value is a list
            of length one containing the name of the Analytics action.  Like:

            {u'actions': [u'ActionName'],
            u'correlation_id': u'00587eca742111e584c50242ac11001b',
            u'path': u'/',
            ...
            }

        """
        action_names = data.get('actions')
        if action_names and isinstance(action_names, list):
            action_name = action_names[0]
        # Format the analytics so they're easy to read in tikibar
        self.metrics['analytics'].append((
            action_name,
            format_dict_as_lines(data),
        ))
        # Record a raw form of the data for JSON export
        self.metrics['analytics_raw'].append({action_name: data})
        self.add_metric(
            Metric(
                timestamp=time.time(),
                type='analytics',
                value='%s: %s' % (action_name, format_dict_as_lines(data)),
                duration=None,
                props={
                    'action_name': action_name,
                    'data': data,
                },
            )
        )

    def write_metrics(self):
        # If the metrics seem too long, start dropping parts to try and fit
        if len(repr(self.metrics)) > self.max_size:
            self.metrics["loglines"] = [("ERROR", "Logs too big for memcached")]
        if len(repr(self.metrics)) > self.max_size:
            self.metrics["queries"]["SQL"] = [
                (
                    query_type,
                    re.sub(r"/\*.+\*/", "", val)[:50] + "...",
                    needs_format,
                    timing,
                )
                for query_type, val, needs_format, timing in self.metrics["queries"]["SQL"]
            ]
        if len(repr(self.metrics)) > self.max_size:
            self.metrics["queries"]["SQL"] = [
                (query_type, "", needs_format, timing)
                for query_type, val, needs_format, timing in self.metrics["queries"]["SQL"]
            ]
        publish_toolbar_metrics(self.correlation_id, self.metrics)
        publish_metrics_list(self.correlation_id, self.metrics_list)
