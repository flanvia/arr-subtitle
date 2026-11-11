import io
import stat
import struct
import unittest
import zipfile
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from arr_subtitle.models import Anime, Attachment, Budget, Candidate, Cancelled, Caption, CollectorError, Post
from arr_subtitle.numbering import Numbering, NumberingRegistry, from_caption, source_key
from arr_subtitle.subtitles import (
    archive_entries, attachment_title, episode, episodes, extract, inspect,
    payload, post_episodes, validate, validate_identity,
)


SAMI = '<SAMI><HEAD><TITLE>Test</TITLE></HEAD><BODY><SYNC Start=0><P Class=KRCC>한글 자막</BODY></SAMI>'
SRT = '1\n00:00:01,000 --> 00:00:02,000\n한글 자막\n'
ASS = '[Script Info]\nTitle: Test\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,한글 자막\n'
ANIME = Anime(1, '테스트', 'Test', ('테스트', 'Test'), 1)
ATTACHMENT = Attachment('https://files.example.com/subtitle', 'Test 01.smi', 'https://example.com/1')


def make_zip(*entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


class Session:
    def __init__(self, data, filename=None):
        self.response = SimpleNamespace(url=ATTACHMENT.url, data=data, content_type='', filename=filename)
        self.calls = []

    def get(self, url, budget, limit=4194304):
        budget.check()
        self.calls.append((url, limit))
        return self.response


class EpisodeTests(unittest.TestCase):
    def test_normalizes_episode_numbers(self):
        for value, expected in [('01', '1'), ('12.50', '12.5'), ('0', '0'), ('0.00', '0'), (Decimal('10.00'), '10')]:
            with self.subTest(value=value):
                self.assertEqual(episode(value), expected)

    def test_rejects_invalid_episode_numbers(self):
        for value in ('1e3', '-1', '1.2.3', 'NaN', 'Infinity', '', True, '100000'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                episode(value)

    def test_generic_release_suffix_and_resolution(self):
        cases = {
            'Series - 01CustomGroup.smi': ('1',),
            'Series - 12AnotherSource.srt': ('12',),
            'Series S02E03.1080p.ass': ('3',),
            '86 - 01.smi': ('1',),
            'Series 01 1080p.ass': ('1',),
            'Series 01.1080p.ass': ('1',),
            'Test.S01E02.1080p.smi': ('2',),
            'Series 12.5.srt': ('12.5',),
            'Series 2기 - 01.smi': ('1',),
            'Series S02.smi': (),
            'Series 시즌2.smi': (),
            'Series 1080p.smi': (),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(episodes(name), expected)

    def test_serial_multiple_episodes_are_preserved(self):
        self.assertEqual(episodes('Series S01E01E02.ass'), ('1', '2'))

    def test_serial_resolution_suffix_is_not_a_decimal_episode(self):
        for name, expected in (
            ('Series.S02E06.1080p.smi', ('6',)),
            ('Series.S02E06.720p.ass', ('6',)),
            ('Series.S02E06E07.1080p.srt', ('6', '7')),
            ('Series.S02E06.5.1080p.ass', ('6.5',)),
        ):
            with self.subTest(name=name):
                self.assertEqual(episodes(name), expected)

    def test_ranges_labels_and_label_requirement(self):
        for text in ('Series 1-3화', 'Series 01~03.zip', 'Series 1 - 3화'):
            with self.subTest(text=text):
                self.assertEqual(episodes(text), ('1', '2', '3'))
        self.assertEqual(episodes('Series EP 02 and Episode 3'), ('2', '3'))
        self.assertEqual(episodes('Series 2', require_label=True), ())
        self.assertEqual(episodes('Series 제2화', require_label=True), ('2',))
        for text in ('Series 3-1화', 'Series 1-1002화'):
            with self.subTest(text=text), self.assertRaises(CollectorError):
                episodes(text)

    def test_numeric_title_is_removed_once(self):
        anime = Anime(86, '86', '86', ('86',))
        self.assertEqual(post_episodes('86 86화', anime), ('86',))
        self.assertEqual(post_episodes('86', anime), ())
        self.assertEqual(post_episodes('Series 2000', Anime(1, 'Series 2000')), ())
        validate_identity('86 - 01.smi', anime, '1')

    def test_title_separators_are_matched_without_changing_punctuation(self):
        anime = Anime(1, 'Series.2024')
        self.assertEqual(post_episodes('Series.2024 2024화', anime), ('2024',))
        validate_identity('Series.2024 - 01.smi', anime, '1')

    def test_titles_and_season_labels_do_not_pollute_identity(self):
        anime = Anime(1, '그녀도 여친 2기', titles=('그녀도 여친 2기',), season=2)
        validate_identity('그녀도 여친 시즌2 제10화 (Custom).smi', anime, '10')
        self.assertEqual(attachment_title('[Anything] Series - 01Custom.smi'), 'Series')

    def test_rejects_wrong_episode_title_and_season(self):
        for name in ('Other Anime 01.smi', 'Test 02.smi', 'Test S02E01.smi', 'Season 2/01.smi', 'Series 01.smi'):
            with self.subTest(name=name), self.assertRaises(CollectorError):
                validate_identity(name, ANIME, '1')

    def test_punctuation_distinguishes_sequels(self):
        anime = Anime(1, 'Working!!!', season=3)
        for name in ('Working!! 01.smi', "Working'!! 01.smi"):
            with self.subTest(name=name), self.assertRaises(CollectorError):
                validate_identity(name, anime, '1')

    def test_allows_leading_title_phrases_with_four_nonseparator_characters(self):
        validate_identity('흉란영애 06.smi', Anime(1, '흉란영애 이야기를 시작합니다'), '6')
        validate_identity('Long 01.smi', Anime(1, 'Long Series Name'), '1')
        validate_identity('Long Name 01.smi', Anime(1, 'Long Name More Words'), '1')

    def test_rejects_short_unrelated_and_nonboundary_title_prefixes(self):
        for filename, title in (
            ('Lon 01.smi', 'Lon Series Name'),
            ('A B C 01.smi', 'A B C Series Name'),
            ('Other 01.smi', 'Long Series Name'),
            ('Long 01.smi', 'Longer Series Name'),
            ('Working!! 01.smi', 'Working!!! More Words'),
            ('Working 01.smi', 'Working!!! More Words'),
            ("Working'!! 01.smi", 'Working!!! More Words'),
        ):
            with self.subTest(filename=filename, title=title), self.assertRaises(CollectorError):
                validate_identity(filename, Anime(1, title), '1')

    def test_leading_title_phrase_does_not_cross_seasons(self):
        anime = Anime(1, 'Long Series Name 2기', season=2)
        for name in ('Long 01.smi', 'Long 시즌1 01.smi', 'Long S01E01.smi'):
            with self.subTest(name=name), self.assertRaises(CollectorError):
                validate_identity(name, anime, '1')
        validate_identity('Long 시즌2 01.smi', anime, '1')


class SubtitleValidationTests(unittest.TestCase):
    def test_accepts_utf8_utf16_and_cp949_sami(self):
        for encoding in ('utf-8', 'utf-8-sig', 'utf-16', 'utf-16-be', 'cp949'):
            data = SAMI.encode(encoding)
            if encoding == 'utf-16-be':
                data = b'\xfe\xff' + data
            with self.subTest(encoding=encoding):
                text, detected = validate(data, '.smi')
                self.assertIn('한글', text)
                self.assertIn(detected, ('utf-8', 'utf-16', 'cp949'))

    def test_accepts_srt_ass_and_ssa(self):
        for data, extension in ((SRT, '.srt'), (ASS, '.ass'), (ASS, '.ssa')):
            with self.subTest(extension=extension):
                self.assertIn('한글', validate(data.encode(), extension)[0])

    def test_rejects_html_error_and_embedded_subtitle_structure(self):
        cases = [
            ('<html>자막 다운로드 오류</html>', '.smi'),
            ("<html><title>로그인 실패</title><script>const sample = '<SAMI><SYNC Start=0>';</script></html>", '.smi'),
            ('<html><body>' + SRT + '</body></html>', '.srt'),
            ('<html><body>' + SAMI + '</body></html>', '.smi'),
            ('<SAMI><SYNC Start=0><P><script>한글</script></SAMI>', '.smi'),
            ('<SAMI><SYNC Start=oops><P>한글</SAMI>', '.smi'),
            ('<SAMI><SYNC Start=0><P>English only</SAMI>', '.smi'),
            (SRT, '.ass'),
        ]
        for text, extension in cases:
            with self.subTest(text=text), self.assertRaises(CollectorError):
                validate(text.encode(), extension)

    def test_rejects_invalid_size_encoding_and_binary(self):
        for data in (b'', b'\xff', b'\xff\xfe\x01', b'x' * (10 * 1024 * 1024 + 1), SAMI.encode() + b'\0'):
            with self.subTest(size=len(data)), self.assertRaises(CollectorError):
                validate(data, '.smi')


class ExtractionTests(unittest.TestCase):
    def test_preserves_real_name_and_removes_size_label(self):
        post = Post('https://example.com/post', 'Test', "<a href='https://cdn.example.com/13.zip?name=generic.zip'>Test 13.zip 0.01MB</a>")
        self.assertEqual(extract(post)[0].filename, 'Test 13.zip')

    def test_drive_links_preserve_visible_title_and_range(self):
        post = Post('https://example.com/post', 'Test', "<a href='https://drive.google.com/file/d/abc123/view'>Test 1-13 Custom</a><a href='https://drive.google.com/drive/folders/xyz'>folder</a>")
        attachments = extract(post)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].filename, 'Test 1-13 Custom')
        self.assertEqual(attachments[0].url, 'https://drive.google.com/uc?export=download&id=abc123')

    def test_extracts_relative_links_and_filename_attributes(self):
        post = Post('https://example.com/post', 'Test', "<a href='/download?id=1' data-origin-name='Test 01.srt'>download</a><a href='/Test%2002.smi'>download</a><a href='javascript:alert(1)' download='Test.smi'>bad</a><a href='http://user:pass@example.com/a.smi'>bad</a>")
        self.assertEqual([a.filename for a in extract(post)], ['Test 01.srt', 'Test 02.smi'])


class ArchiveTests(unittest.TestCase):
    def test_preserves_full_member_path(self):
        entries = archive_entries(make_zip(('Season 1/Test 01.smi', SAMI.encode())))
        self.assertEqual(entries, [('Season 1/Test 01.smi', SAMI.encode())])

    def test_rejects_unsafe_members(self):
        names = ('../Test 01.smi', '/Test 01.smi', 'C:\\Test 01.smi', 'folder/../Test 01.smi')
        for name in names:
            with self.subTest(name=name), self.assertRaises(CollectorError):
                archive_entries(make_zip((name, SAMI.encode())))

    def test_rejects_duplicate_normalized_names(self):
        for other in ('test 01.smi', './Test 01.smi', 'folder/../Test 01.smi'):
            with self.subTest(other=other), self.assertRaises(CollectorError):
                archive_entries(make_zip(('Test 01.smi', SAMI.encode()), (other, SAMI.encode())))

    def test_rejects_symlinks_and_nested_archives(self):
        info = zipfile.ZipInfo('Test 01.smi')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        for data in (make_zip((info, b'target')), make_zip(('inner.zip', b'PK'), ('Test 01.smi', SAMI.encode()))):
            with self.subTest(size=len(data)), self.assertRaises(CollectorError):
                archive_entries(data)

    def test_rejects_member_count_and_expanded_size_limits(self):
        data = make_zip(*[(f'{number}.txt', b'') for number in range(501)])
        with self.assertRaises(CollectorError):
            archive_entries(data)
        data = bytearray(make_zip(('Test 01.smi', SAMI.encode())))
        central = data.index(b'PK\x01\x02')
        struct.pack_into('<I', data, central + 24, 101 * 1024 * 1024)
        with self.assertRaises(CollectorError):
            archive_entries(bytes(data))

    def test_decodes_cp949_member_names(self):
        original = '테스트 01.smi'
        raw = original.encode('cp949')
        placeholder = 'x' * len(raw)
        data = make_zip((placeholder, SAMI.encode()))
        data = data.replace(placeholder.encode(), raw)
        self.assertEqual(archive_entries(data)[0][0], original)

    def test_utf8_member_names_and_corrupt_zip(self):
        self.assertEqual(archive_entries(make_zip(('테스트 01.smi', SAMI.encode())))[0][0], '테스트 01.smi')
        data = bytearray(make_zip(('Test 01.smi', SAMI.encode())))
        data[45] ^= 0xFF
        with self.assertRaises(CollectorError):
            archive_entries(bytes(data))

    def test_rejects_embedded_null_member_name(self):
        data = make_zip(('Testx01.smi', SAMI.encode())).replace(b'Testx01.smi', b'Test\x0001.smi')
        with self.assertRaises(CollectorError):
            archive_entries(data)

    def test_ignores_auxiliary_files_but_validates_subtitle_contents(self):
        data = make_zip(('OP.smi', b'not a subtitle'), ('Test 01.smi', SAMI.encode()))
        self.assertEqual(len(archive_entries(data)), 1)
        with self.assertRaises(CollectorError):
            archive_entries(make_zip(('Test 01.smi', b'<html>error</html>')))


class InspectionTests(unittest.TestCase):
    def test_named_direct_subtitle_does_not_download(self):
        session = Session(b'not downloaded')
        self.assertEqual(inspect(session, ATTACHMENT, ('1',), Budget()), [('Test 01.smi', ('1',), None)])
        self.assertEqual(session.calls, [])

    def test_cancelled_budget_stops_inspection(self):
        budget = Budget()
        budget.cancel.set()
        with self.assertRaises(Cancelled):
            inspect(Session(b''), ATTACHMENT, ('1',), budget)

    def test_direct_subtitle_inherits_only_unique_post_episode(self):
        attachment = replace(ATTACHMENT, filename='Test.smi')
        self.assertEqual(inspect(Session(b''), attachment, ('4',), Budget())[0][1], ('4',))
        self.assertEqual(inspect(Session(b''), attachment, ('1', '2'), Budget())[0][1], ())

    def test_rejects_single_post_episode_conflict(self):
        with self.assertRaises(CollectorError):
            inspect(Session(b''), ATTACHMENT, ('2',), Budget())

    def test_single_unnumbered_zip_member_inherits_unique_post(self):
        attachment = replace(ATTACHMENT, filename='Test.zip')
        data = make_zip(('directory/Test.smi', SAMI.encode()))
        self.assertEqual(inspect(Session(data), attachment, ('4',), Budget()), [('directory/Test.smi', ('4',), 'directory/Test.smi')])
        self.assertEqual(inspect(Session(data), attachment, ('1', '2'), Budget())[0][1], ())

    def test_multiple_unnumbered_zip_members_do_not_inherit(self):
        data = make_zip(('a/Test.smi', SAMI.encode()), ('b/Test.smi', SAMI.encode()))
        results = inspect(Session(data), replace(ATTACHMENT, filename='Test.zip'), ('4',), Budget())
        self.assertTrue(all(not numbers for _, numbers, _ in results))

    def test_zip_range_is_not_claimed_without_members(self):
        data = make_zip(('Test 01.smi', SAMI.encode()))
        result = inspect(Session(data), replace(ATTACHMENT, filename='Test 1-3.zip'), ('1', '2', '3'), Budget())
        self.assertEqual(result[0][1], ('1',))

    def test_multi_episode_zip_is_not_limited_to_representative_post_episode(self):
        data = make_zip(*[(f'Test {number:02}.smi', SAMI.encode()) for number in range(1, 14)])
        results = inspect(Session(data), replace(ATTACHMENT, filename='Test 1-13.zip'), ('13',), Budget())
        self.assertEqual([numbers for _, numbers, _ in results], [(str(number),) for number in range(1, 14)])

    def test_single_number_zip_still_rejects_post_conflict(self):
        for entries in (
            [('Test 01.smi', SAMI.encode())],
            [('a/Test 01.smi', SAMI.encode()), ('b/Test 01.smi', SAMI.encode())],
        ):
            with self.subTest(count=len(entries)), self.assertRaises(CollectorError):
                inspect(Session(make_zip(*entries)), replace(ATTACHMENT, filename='Test.zip'), ('2',), Budget())

    def test_unknown_zip_member_does_not_inherit_from_multi_episode_archive(self):
        data = make_zip(('Test 01.smi', SAMI.encode()), ('Test 13.smi', SAMI.encode()), ('Test.smi', SAMI.encode()))
        results = inspect(Session(data), replace(ATTACHMENT, filename='Test.zip'), ('13',), Budget())
        self.assertEqual([numbers for _, numbers, _ in results], [('1',), ('13',), ()])

    def test_opaque_download_uses_actual_format_and_response_name(self):
        attachment = replace(ATTACHMENT, filename='Test 자막')
        session = Session(SAMI.encode(), 'Test 03.smi')
        self.assertEqual(inspect(session, attachment, (), Budget()), [('Test 03.smi', ('3',), None)])
        with self.assertRaises(CollectorError):
            inspect(Session(SAMI.encode(), 'Test 04.smi'), replace(attachment, filename='Test 03'), (), Budget())

    def test_html_cannot_confirm_declared_range(self):
        with self.assertRaises(CollectorError):
            inspect(Session(b'<html>error</html>'), replace(ATTACHMENT, filename='Test 1-12.zip'), tuple(map(str, range(1, 13))), Budget())


class PayloadTests(unittest.TestCase):
    def candidate(self, **kwargs):
        return Candidate(ANIME, '1', 'Maker', ATTACHMENT.page, ATTACHMENT, **kwargs)

    def test_selects_requested_member_and_preserves_full_path(self):
        data = make_zip(('folder/Test 01.smi', SAMI.encode()), ('folder/Test 02.smi', SAMI.encode()))
        self.assertEqual(payload(Session(data), self.candidate(member='folder/Test 01.smi'), Budget()), (SAMI.encode(), '.smi', 'folder/Test 01.smi'))

    def test_cumulative_numbering_selects_source_episode(self):
        candidate = self.candidate(member='Test 13.smi', source_episode='13', numbering_evidence='caption 1 = source 13')
        data = make_zip(('Test 13.smi', SAMI.encode()), ('Test 01.smi', SAMI.encode()))
        self.assertEqual(payload(Session(data), candidate, Budget())[2], 'Test 13.smi')

    def test_rejects_changed_member_and_conflicting_identity(self):
        for name in ('Other 01.smi', 'Test 02.smi', 'Season 2/01.smi'):
            with self.subTest(name=name), self.assertRaises(CollectorError):
                payload(Session(make_zip((name, SAMI.encode()))), self.candidate(member=name), Budget())
        with self.assertRaises(CollectorError):
            payload(Session(make_zip(('Test 02.smi', SAMI.encode()))), self.candidate(member='Test 01.smi'), Budget())

    def test_unknown_member_in_multi_file_archive_is_not_assigned(self):
        data = make_zip(('Test.smi', SAMI.encode()), ('Test 02.smi', SAMI.encode()))
        with self.assertRaises(CollectorError):
            payload(Session(data), self.candidate(member='Test.smi'), Budget())

    def test_direct_response_name_must_not_conflict(self):
        with self.assertRaises(CollectorError):
            payload(Session(SAMI.encode(), 'Test 02.smi'), self.candidate(), Budget())

    def test_actual_format_is_detected_instead_of_trusting_suffix(self):
        self.assertEqual(payload(Session(SRT.encode()), self.candidate(), Budget())[1], '.srt')


class NumberingTests(unittest.TestCase):
    def post(self, title='Test 24화', individual='24'):
        return Post('https://example.com/24', title, f"<a href='https://files.example.com/all.zip'>Test 01-24.zip</a><a href='https://files.example.com/one.zip'>Test {individual}.zip</a>")

    def test_caption_post_and_individual_attachment_establish_offset(self):
        caption = Caption('12', 'https://example.com/24', 'Maker')
        numbering = from_caption(caption, self.post(), 2, 12, ANIME)
        self.assertEqual(numbering.offset, 12)
        self.assertEqual(numbering.to_season('13'), '1')
        self.assertEqual(numbering.to_season('24'), '12')
        self.assertIsNone(numbering.to_season('12'))
        self.assertIsNone(numbering.to_season('25'))
        self.assertEqual(numbering.to_source('1'), '13')

    def test_batch_maximum_and_conflicting_attachments_do_not_prove_offset(self):
        caption = Caption('12', 'https://example.com/24')
        for post in (replace(self.post(), title='Test 전체 자막'), self.post(individual='23'), self.post(individual='01-24')):
            with self.subTest(title=post.title):
                self.assertIsNone(from_caption(caption, post, 2, 12))

    def test_caption_must_reference_the_exact_post(self):
        self.assertIsNone(from_caption(Caption('12', 'https://example.com/other'), self.post(), 2, 12))

    def test_unrelated_post_and_attachment_do_not_establish_offset(self):
        caption = Caption('12', 'https://example.com/24')
        post = self.post()
        self.assertIsNone(from_caption(caption, replace(post, title='Other 24화'), 2, 12, ANIME))
        self.assertIsNone(from_caption(caption, replace(post, html=post.html.replace('Test 24.zip', 'Other 24.zip')), 2, 12, ANIME))

    def test_conflicting_offset_permanently_invalidates_source_only(self):
        registry = NumberingRegistry()
        first = Numbering(12, Decimal(12), 'first')
        self.assertTrue(registry.register('source', first))
        self.assertTrue(registry.register('other', first))
        self.assertFalse(registry.register('source', Numbering(13, Decimal(12), 'second')))
        self.assertIsNone(registry.get('source'))
        self.assertTrue(registry.has_conflict('source'))
        self.assertFalse(registry.register('source', first))
        self.assertEqual(registry.get('other'), first)

    def test_source_keys_separate_blogs_and_categories(self):
        first = SimpleNamespace(platform='blogspot', id='a.example.com')
        second = SimpleNamespace(platform='blogspot', id='b.example.com')
        self.assertNotEqual(source_key(first), source_key(second))
        self.assertNotEqual(source_key(first), source_key(first, '/category/one'))

    def test_negative_and_fractional_correspondence_is_rejected(self):
        for caption in (Caption('25', 'https://example.com/24'), Caption('12.5', 'https://example.com/24')):
            with self.subTest(caption=caption), self.assertRaises(CollectorError):
                from_caption(caption, self.post(), 2, 12)


if __name__ == '__main__':
    unittest.main()
