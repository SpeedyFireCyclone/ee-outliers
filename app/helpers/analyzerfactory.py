from helpers.singletons import logging
import configparser
import os
import re

from analyzers.metrics import MetricsAnalyzer
from analyzers.simplequery import SimplequeryAnalyzer
from analyzers.terms import TermsAnalyzer
from analyzers.word2vec import Word2VecAnalyzer
from analyzers.word2vec_dev import Word2VecDevAnalyzer

CLASS_MAPPING = {
    "simplequery": SimplequeryAnalyzer,
    "metrics": MetricsAnalyzer,
    "word2vec": Word2VecAnalyzer,
    "word2vec_dev": Word2VecDevAnalyzer,
    "terms": TermsAnalyzer
}


class AnalyzerFactory:

    @staticmethod
    def section_to_analyzer(section_name, section):
        """
        Converts the name of a section in the configuration file into
        :param section_name: name of section in the parsed configuration file, i.e. metrics_numerical_value_dummy_test
        :param section: the section object representing the section called section_name
        :return:
        """
        # Check if the config section matches any of the defined "analyzer" prefixes
        for prefix, _class in CLASS_MAPPING.items():
            if section_name.startswith(prefix):
                return _class(model_name=section_name[(len(prefix) + 1):], config_section=section)
        return None

    @staticmethod
    def create(config_file):
        """
        Creates an analyzer based on a configuration file
        :param config_file: configuration file containing a single analyzer
        :return: returns the analyzer object
        """
        if not os.path.isfile(config_file):
            raise ValueError("Use case file %s does not exist" % config_file)

        # Read the ini file from disk
        config = configparser.RawConfigParser()
        config.read(config_file)

        logging.logger.debug(config)

        # Create a list of all analyzers found in the config file
        analyzers = [AnalyzerFactory.section_to_analyzer(section_name, section)
                     for section_name, section in config.items()]
        analyzers = list(filter(None, analyzers))

        # File should only contain 1 analyzer
        if len(analyzers) != 1:
            raise ValueError("Config file must contain exactly one use case (found %d)" % len(analyzers))

        analyzer = analyzers[0]

        if "whitelist_literals" in config.sections():
            for _, value in config["whitelist_literals"].items():
                analyzer.model_whitelist_literals.append(set([x.strip() for x in value.split(",")]))

        if "whitelist_regexps" in config.sections():
            for _, value in config["whitelist_regexps"].items():
                analyzer.model_whitelist_regexps.append((set([re.compile(x.strip(), re.IGNORECASE)
                                                              for x in value.split(",")])))

        return analyzer
