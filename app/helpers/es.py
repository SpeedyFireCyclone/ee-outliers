from elasticsearch import helpers as eshelpers, Elasticsearch
import datetime
import helpers.utils
import helpers.logging
import json
import datetime as dt
from pygrok import Grok
import math

from helpers.singleton import singleton
from helpers.notifier import Notifier
from helpers.outlier import Outlier
from collections import defaultdict
from itertools import chain


@singleton
class ES:
    index = None
    conn = None
    settings = None

    grok_filters = dict()

    notifier = None

    bulk_actions = []

    BULK_FLUSH_SIZE = 1000

    def __init__(self, settings=None, logging=None):
        self.settings = settings
        self.logging = logging

        if self.settings.config.getboolean("notifier", "email_notifier"):
            self.notifier = Notifier(settings, logging)

    def init_connection(self):
        self.conn = Elasticsearch([self.settings.config.get("general", "es_url")], use_ssl=False,
                                  timeout=self.settings.config.getint("general", "es_timeout"),
                                  verify_certs=False, retry_on_timeout=True)

        if self.conn.ping():
            self.logging.logger.info("connected to Elasticsearch on host %s" %
                                     (self.settings.config.get("general", "es_url")))
        else:
            self.logging.logger.error("could not connect to to host %s. Exiting!" %
                                      (self.settings.config.get("general", "es_url")))

        return self.conn

    def _get_history_window(self, model_settings=None):
        if model_settings is None:
            timestamp_field = self.settings.config.get("general", "timestamp_field", fallback="timestamp")
            history_window_days = self.settings.config.getint("general", "history_window_days")
            history_window_hours = self.settings.config.getint("general", "history_window_hours")
        else:
            timestamp_field = model_settings["timestamp_field"]
            history_window_days = model_settings["history_window_days"]
            history_window_hours = model_settings["history_window_hours"]
        return timestamp_field, history_window_days, history_window_hours

    def _scan(self, index, search_range, bool_clause=None, sort_clause=None, query_fields=None, search_query=None,
              model_settings=None):

        preserve_order = False

        if model_settings is not None and model_settings["process_documents_chronologically"]:
            sort_clause = {"sort": [{model_settings["timestamp_field"]: "desc"}]}
            preserve_order = True

        return eshelpers.scan(self.conn, request_timeout=self.settings.config.getint("general", "es_timeout"),
                              index=index, query=build_search_query(bool_clause=bool_clause,
                                                                    sort_clause=sort_clause,
                                                                    search_range=search_range,
                                                                    query_fields=query_fields,
                                                                    search_query=search_query),
                              size=self.settings.config.getint("general", "es_scan_size"),
                              scroll=self.settings.config.get("general", "es_scroll_time"),
                              preserve_order=preserve_order, raise_on_error=False)

    def _count_documents(self, index, search_range, bool_clause=None, query_fields=None, search_query=None):
        res = self.conn.search(index=index, body=build_search_query(bool_clause=bool_clause, search_range=search_range,
                                                                    query_fields=query_fields,
                                                                    search_query=search_query),
                               size=self.settings.config.getint("general", "es_scan_size"),
                               scroll=self.settings.config.get("general", "es_scroll_time"))
        result = res["hits"]["total"]

        # Result depend of the version of ElasticSearch (> 7, the result is a dictionary)
        if isinstance(result, dict):
            return result["value"]
        else:
            return result

    def count_and_scan_documents(self, index, bool_clause=None, sort_clause=None, query_fields=None, search_query=None,
                                 model_settings=None):
        timestamp_field, history_window_days, history_window_hours = self._get_history_window(model_settings)
        search_range = self.get_time_filter(days=history_window_days, hours=history_window_hours,
                                            timestamp_field=timestamp_field)
        total_events = self._count_documents(index, search_range, bool_clause, query_fields, search_query)
        if total_events > 0:
            return total_events, self._scan(index, search_range, bool_clause, sort_clause, query_fields, search_query,
                                            model_settings)
        return total_events, []

    @staticmethod
    def filter_by_query_string(query_string=None):
        filter_clause = {"filter": [
            {"query_string": {"query": query_string}}
        ]}

        return filter_clause

    @staticmethod
    def filter_by_dsl_query(dsl_query=None):
        dsl_query = json.loads(dsl_query)

        if isinstance(dsl_query, list):
            filter_clause = {"filter": []}
            for query in dsl_query:
                filter_clause["filter"].append(query["query"])
        else:
            filter_clause = {"filter": [
                dsl_query["query"]
            ]}
        return filter_clause

    # this is part of housekeeping, so we should not access non-threat-save objects, such as logging progress to
    # the console using ticks!
    def remove_all_whitelisted_outliers(self):
        outliers_filter_query = {"filter": [{"term": {"tags": "outlier"}}]}

        total_outliers_whitelisted = 0
        total_outliers_processed = 0

        idx = self.settings.config.get("general", "es_index_pattern")
        total_nr_outliers, documents = self.count_and_scan_documents(index=idx, bool_clause=outliers_filter_query)

        if total_nr_outliers > 0:
            self.logging.logger.info("going to analyze %s outliers and remove all whitelisted items", "{:,}"
                                     .format(total_nr_outliers))
            start_time = dt.datetime.today().timestamp()

            for doc in documents:
                total_outliers_processed = total_outliers_processed + 1
                total_outliers_in_doc = int(doc["_source"]["outliers"]["total_outliers"])
                # generate all outlier objects for this document
                total_whitelisted = 0

                for i in range(total_outliers_in_doc):
                    outlier_type = doc["_source"]["outliers"]["type"][i]
                    outlier_reason = doc["_source"]["outliers"]["reason"][i]
                    outlier_summary = doc["_source"]["outliers"]["summary"][i]

                    outlier = Outlier(outlier_type=outlier_type, outlier_reason=outlier_reason,
                                      outlier_summary=outlier_summary, doc=doc)
                    if outlier.is_whitelisted():
                        total_whitelisted += 1

                # if all outliers for this document are whitelisted, removed them all. If not, don't touch the document.
                # this is a limitation in the way our outliers are stored: if not ALL of them are whitelisted, we
                # can't remove just the whitelisted ones
                # from the Elasticsearch event, as they are stored as array elements and potentially contain
                # observations that should be removed, too.
                # In this case, just don't touch the document.
                if total_whitelisted == total_outliers_in_doc:
                    total_outliers_whitelisted += 1
                    doc = remove_outliers_from_document(doc)
                    self.add_update_bulk_action(doc)

                # we don't use the ticker from the logger singleton, as this will be called from the housekeeping thread
                # if we share a same ticker between multiple threads, strange results would start to appear in
                # progress logging
                # so, we duplicate part of the functionality from the logger singleton
                if self.logging.verbosity >= 5:
                    should_log = True
                else:
                    should_log = total_outliers_processed % max(1,
                                                                int(math.pow(10, (6 - self.logging.verbosity)))) == 0 \
                                 or total_outliers_processed == total_nr_outliers

                if should_log:
                    # avoid a division by zero
                    time_diff = max(float(1), float(dt.datetime.today().timestamp() - start_time))
                    ticks_per_second = "{:,}".format(round(float(total_outliers_processed) / time_diff))

                    self.logging.logger.info("whitelisting historical outliers " + " [" + ticks_per_second + " eps." +
                                             " - " + '{:.2f}'.format(round(float(total_outliers_processed) /
                                                                           float(total_nr_outliers) * 100, 2)) +
                                             "% done" + " - " + "{:,}".format(total_outliers_whitelisted) +
                                             " outliers whitelisted]")

            self.flush_bulk_actions()

        return total_outliers_whitelisted

    def remove_all_outliers(self):
        idx = self.settings.config.get("general", "es_index_pattern")

        must_clause = {"filter": [{"term": {"tags": "outlier"}}]}
        timestamp_field, history_window_days, history_window_hours = self._get_history_window()
        search_range = self.get_time_filter(days=history_window_days, hours=history_window_hours,
                                            timestamp_field=timestamp_field)
        total_outliers = self._count_documents(index=idx, search_range=search_range, bool_clause=must_clause)

        if total_outliers > 0:
            query = build_search_query(bool_clause=must_clause, search_range=search_range)

            script = {
                "source": "ctx._source.remove(\"outliers\"); " +
                          "ctx._source.tags.remove(ctx._source.tags.indexOf(\"outlier\"))",
                "lang": "painless"
            }

            query["script"] = script

            self.logging.logger.info("wiping %s existing outliers", "{:,}".format(total_outliers))
            self.conn.update_by_query(index=idx, body=query, refresh=True, wait_for_completion=True)
            self.logging.logger.info("wiped outlier information of " + "{:,}".format(total_outliers) + " documents")
        else:
            self.logging.logger.info("no existing outliers were found, so nothing was wiped")

    def process_outlier(self, outlier=None, should_notify=False):
        if outlier.is_whitelisted():
            if self.settings.config.getboolean("general", "print_outliers_to_console"):
                self.logging.logger.info(outlier.outlier_dict["summary"] + " [whitelisted outlier]")
            return False
        else:
            if self.settings.config.getboolean("general", "es_save_results"):
                self.save_outlier(outlier=outlier)

            if should_notify:
                self.notifier.notify_on_outlier(outlier=outlier)

            if self.settings.config.getboolean("general", "print_outliers_to_console"):
                self.logging.logger.info("outlier - " + outlier.outlier_dict["summary"])
            return True

    def add_update_bulk_action(self, document):
        action = {
            '_op_type': 'update',
            '_index': document["_index"],
            '_type': document["_type"],
            '_id': document["_id"],
            'retry_on_conflict': 10,
            'doc': document["_source"]
        }
        self.add_bulk_action(action)

    def add_bulk_action(self, action):
        self.bulk_actions.append(action)
        if len(self.bulk_actions) > self.BULK_FLUSH_SIZE:
            self.flush_bulk_actions()

    def flush_bulk_actions(self, refresh=False):
        if len(self.bulk_actions) == 0:
            return
        eshelpers.bulk(self.conn, self.bulk_actions, stats_only=True, refresh=refresh)
        self.bulk_actions = []

    def save_outlier(self, outlier=None):
        # add the derived fields as outlier observations
        derived_fields = self.extract_derived_fields(outlier.doc["_source"])
        for derived_field, derived_value in derived_fields.items():
            outlier.outlier_dict["derived_" + derived_field] = derived_value

        doc = add_outlier_to_document(outlier)
        self.add_update_bulk_action(doc)

    def extract_derived_fields(self, doc_fields):
        derived_fields = dict()
        for field_name, grok_pattern in self.settings.config.items("derivedfields"):
            try:
                # If key doesn't exist, an exception is raise
                doc_value = helpers.utils.get_dotkey_value(doc_fields, field_name, case_sensitive=False)

                if grok_pattern in self.grok_filters.keys():
                    grok = self.grok_filters[grok_pattern]
                else:
                    grok = Grok(grok_pattern)
                    self.grok_filters[grok_pattern] = grok

                match_dict = grok.match(doc_value)

                if match_dict:
                    for match_dict_k, match_dict_v in match_dict.items():
                        derived_fields[match_dict_k] = match_dict_v

            except KeyError:
                pass  # Ignore, value not found...

        return derived_fields

    def extract_fields_from_document(self, doc, extract_derived_fields=False):
        doc_fields = doc["_source"]

        if extract_derived_fields:
            derived_fields = self.extract_derived_fields(doc_fields)

            for k, v in derived_fields.items():
                doc_fields[k] = v

        return doc_fields

    @staticmethod
    def get_time_filter(days=None, hours=None, timestamp_field="timestamp"):
        time_start = (datetime.datetime.now() - datetime.timedelta(days=days, hours=hours)).isoformat()
        time_stop = datetime.datetime.now().isoformat()

        # Construct absolute time range filter, increases cacheability
        time_filter = {
            "range": {
                str(timestamp_field): {
                    "gte": time_start,
                    "lte": time_stop
                }
            }
        }
        return time_filter


def add_outlier_to_document(outlier):
    doc = add_tag_to_document(outlier.doc, "outlier")

    if "outliers" in doc["_source"]:
        if outlier.outlier_dict["summary"] not in doc["_source"]["outliers"]["summary"]:
            merged_outliers = defaultdict(list)
            for k, v in chain(doc["_source"]["outliers"].items(), outlier.get_outlier_dict_of_arrays().items()):

                # merge ["reason 1"] and ["reason 2"]] into ["reason 1", "reason 2"]
                if isinstance(v, list):
                    merged_outliers[k].extend(v)
                else:
                    merged_outliers[k].append(v)

            merged_outliers["total_outliers"] = doc["_source"]["outliers"]["total_outliers"] + 1
            doc["_source"]["outliers"] = merged_outliers
    else:
        doc["_source"]["outliers"] = outlier.get_outlier_dict_of_arrays()
        doc["_source"]["outliers"]["total_outliers"] = 1

    return doc


def remove_outliers_from_document(doc):
    doc = remove_tag_from_document(doc, "outlier")

    if "outliers" in doc["_source"]:
        doc["_source"].pop("outliers")

    return doc


def add_tag_to_document(doc, tag):
    if "tags" not in doc["_source"]:
        doc["_source"]["tags"] = [tag]
    else:
        if tag not in doc["_source"]["tags"]:
            doc["_source"]["tags"].append(tag)
    return doc


def remove_tag_from_document(doc, tag):
    if "tags" not in doc["_source"]:
        pass
    else:
        tags = doc["_source"]["tags"]
        if tag in tags:
            tags.remove(tag)
            doc["_source"]["tags"] = tags
    return doc


def build_search_query(bool_clause=None, sort_clause=None, search_range=None, query_fields=None, search_query=None):
    query = dict()
    query["query"] = dict()
    query["query"]["bool"] = dict()
    query["query"]["bool"]["filter"] = list()

    if query_fields:
        query["_source"] = query_fields

    if bool_clause:
        # To avoid side effects (multiple search_range) when calling multiple times the function on the same bool_clause
        query["query"]["bool"]["filter"] = bool_clause["filter"].copy()

    if sort_clause:
        query.update(sort_clause)

    if search_range:
        if "bool" not in query["query"]:
            query["bool"] = dict()

        if "filter" not in query["query"]["bool"]:
            query["query"]["bool"]["filter"] = list()
        query["query"]["bool"]["filter"].append(search_range)

    if search_query:
        query["query"]["bool"]["filter"].append(search_query["filter"].copy())

    return query
