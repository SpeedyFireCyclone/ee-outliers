import unittest

import json
import copy

from helpers.singletons import settings
from helpers.housekeeping import HousekeepingJob
from tests.unit_tests.test_stubs.test_stub_es import TestStubEs

test_file_no_whitelist_path_config = "/app/tests/unit_tests/files/housekeeping_no_whitelist.conf"
test_file_whitelist_path_config = "/app/tests/unit_tests/files/whitelist_tests_01.conf"
doc_without_outlier_test_file = json.load(open("/app/tests/unit_tests/files/doc_without_outlier.json"))
doc_with_outlier_test_file = json.load(open("/app/tests/unit_tests/files/doc_with_outlier.json"))


class TestHousekeeping(unittest.TestCase):
    def setUp(self):
        self.test_es = TestStubEs()

    def tearDown(self):
        self.test_es.restore_es()

    def test_housekeeping_correctly_remove_whitelisted_outlier_when_file_modification(self):
        backup_args_config = settings.args.config[:]
        settings.args.config = [test_file_no_whitelist_path_config]
        housekeeping = HousekeepingJob()

        # Add document to "Database"
        doc_with_outlier = copy.deepcopy(doc_with_outlier_test_file)
        self.test_es.add_doc(doc_with_outlier)

        # Update configuration (read new config and append to default)
        with open(test_file_whitelist_path_config, 'r') as test_file:
            filecontent = test_file.read()

        with open(test_file_no_whitelist_path_config, 'a') as test_file:
            test_file.write(filecontent)

        housekeeping.execute_housekeeping()

        # Fetch result
        result = [elem for elem in self.test_es.scan()][0]

        # Compute expected result:
        doc_without_outlier = copy.deepcopy(doc_without_outlier_test_file)

        # Restore configuration
        settings.args.config = backup_args_config

        self.assertEqual(result, doc_without_outlier)

    def test_housekeeping_not_execute_no_whitelist_parameter_change(self):
        backup_args_config = settings.args.config[:]
        settings.args.config = [test_file_no_whitelist_path_config]
        housekeeping = HousekeepingJob()

        # Add document to "Database"
        doc_with_outlier = copy.deepcopy(doc_with_outlier_test_file)
        self.test_es.add_doc(doc_with_outlier)

        # Update configuration (create new section and append to default)
        filecontent = "[dummy_section]\nparam=1"

        with open(test_file_no_whitelist_path_config, 'a') as test_file:
            test_file.write(filecontent)

        housekeeping.execute_housekeeping()

        # Fetch result
        result = [elem for elem in self.test_es.scan()][0]

        # Restore configuration
        settings.args.config = backup_args_config

        self.assertEqual(result, doc_with_outlier)