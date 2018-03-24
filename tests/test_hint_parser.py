import unittest

from britney2.hints import HintParser, single_hint_taking_list_of_packages

from . import MockObject, HINTS_ALL, TEST_HINTER


def new_hint_parser(logger=None):
    if logger is None:
        def empty_logger(x, type='I'):
            pass
        logger = empty_logger
    fake_britney = MockObject(log=logger)
    hint_parser = HintParser(fake_britney)
    return hint_parser


def parse_should_not_call_this_function(*args, **kwargs):
    raise AssertionError("Should not be called")


class HintParsing(unittest.TestCase):

    def test_parse_invalid_hints(self):
        hint_log = []
        hint_parser = new_hint_parser(lambda x, type='I': hint_log.append(x))

        hint_parser.register_hint_type('min-10-arg', parse_should_not_call_this_function, min_args=10)
        hint_parser.register_hint_type('simple-hint', parse_should_not_call_this_function)

        tests = [
            {
                'hint_text': 'min-10-arg foo bar',
                'permissions': HINTS_ALL,
                'error_message_contains': 'Needs at least 10 argument(s), got'
            },
            {
                'hint_text': 'undefined-hint with some arguments',
                'permissions': HINTS_ALL,
                'error_message_contains': 'Unknown hint found in'
            },
            {
                'hint_text': 'simple-hint foo/1.0',
                'permissions': ['not-this-hint'],
                'error_message_contains': 'not a part of the permitted hints for'
            },
        ]

        for test in tests:
            hint_parser.parse_hints(TEST_HINTER, test['permissions'], 'test-parse-hint', [test['hint_text']])
            assert len(hint_log) == 1
            assert test['error_message_contains'] in hint_log[0]
            assert hint_parser.hints.is_empty
            hint_log.clear()

    def test_alias(self):
        hint_parser = new_hint_parser()
        hint_parser.register_hint_type('real-name',
                                       single_hint_taking_list_of_packages,
                                       aliases=['alias1', 'alias2']
                                       )
        hint_parser.parse_hints(TEST_HINTER,
                                HINTS_ALL,
                                'test-parse-hint',
                                [
                                    'alias1 foo/1.0',
                                    'alias2 bar/2.0',
                                ])
        hints = hint_parser.hints
        # Aliased hints can be found by the real name
        assert hints.search(type='real-name', package='foo', version='1.0')
        assert hints.search(type='real-name', package='bar', version='2.0')
        # But not by their alias
        assert not hints.search(type='alias1', package='foo', version='1.0')
        assert not hints.search(type='alias2', package='bar', version='2.0')


if __name__ == '__main__':
    unittest.main()
