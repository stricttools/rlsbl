"""Tests for committed lineage records -- the append-only log of repository-surgery facts.

Covers what the storage layer promises:

- every event kind survives an append -> read round-trip unchanged;
- the location resolver picks the releasable state directory in a monorepo
  workspace and ``.rlsbl/`` in a standalone repo (including a standalone
  successor produced by an extract), and both hold the same format;
- every malformed-record variant is a hard error at the read-for-use site,
  naming the file and the line -- including undecodable bytes and a duplicate
  event id;
- appends accumulate in order across separate calls, and a new event survives a
  torn (newline-less) last line;
- the stamped copies :func:`append_events` returns are top-level copies whose
  nested values are shared with the caller.
"""

import json
import os

import pytest

from rlsbl import lineage
from rlsbl.lineage import (
    CURRENT_FORMAT_VERSION,
    EVENT_KINDS,
    AnchorMapping,
    AnchorRemapEvent,
    BoundaryAlias,
    BoundaryAliasEvent,
    ConversionEvent,
    DepartedGlobsEvent,
    IdentityTransitionEvent,
    LineageEndpoint,
    LineageError,
    PromotionSplitMapEvent,
    SplitMapping,
    TagMapEvent,
    TagMapping,
    append_event,
    append_events,
    get_lineage_path,
    lineage_file_exists,
    read_events,
    serialize_event,
)
from rlsbl.workspace_types import get_releasable_dir


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def make_conversion():
    return ConversionEvent(
        direction="extract",
        source=LineageEndpoint(
            repo=".",
            path="packages/widget",
            project="widget",
            releasable="widget",
            tag_format="{name}@v{version}",
        ),
        destination=LineageEndpoint(
            repo="https://github.com/example/widget",
            tag_format="v{version}",
        ),
        commit=SHA_A,
    )


def make_tag_map():
    return TagMapEvent(
        mappings=[
            TagMapping(
                old_tag="widget@v0.3.0",
                new_tag="v0.3.0",
                old_commit=SHA_A,
                new_commit=SHA_B,
            ),
            TagMapping(old_tag="widget@v0.4.0", new_tag="v0.4.0", new_commit=SHA_C),
        ]
    )


def make_anchor_remap():
    return AnchorRemapEvent(
        rewrite="git-filter-repo --to-subdirectory-filter packages/widget",
        mappings=[
            AnchorMapping(old_sha=SHA_A, new_sha=SHA_B),
            AnchorMapping(old_sha=SHA_C, new_sha=SHA_D),
        ],
    )


def make_departed_globs():
    return DepartedGlobsEvent(
        globs=["widget@v*", "widget-legacy@v*"],
        destination=LineageEndpoint(repo="https://github.com/example/widget"),
    )


def make_boundary_alias():
    return BoundaryAliasEvent(
        aliases=[
            BoundaryAlias(
                alias_tag="widget@v0.4.0", aliased_tag="v0.4.0", commit=SHA_C
            )
        ]
    )


def make_identity_transition():
    return IdentityTransitionEvent(
        facet="go-module-path",
        old="github.com/example/monorepo/packages/widget",
        new="github.com/example/widget",
        effective_version="0.5.0",
    )


def make_promotion_split_map():
    return PromotionSplitMapEvent(
        subtree_path="packages/widget",
        mirror_remote="https://github.com/example/widget-mirror",
        promoted_version="0.5.0",
        mappings=[SplitMapping(source_sha=SHA_A, split_sha=SHA_B)],
    )


ALL_MAKERS = {
    "conversion": make_conversion,
    "tag-map": make_tag_map,
    "anchor-remap": make_anchor_remap,
    "departed-globs": make_departed_globs,
    "boundary-alias": make_boundary_alias,
    "identity-transition": make_identity_transition,
    "promotion-split-map": make_promotion_split_map,
}


class TestEventKindCoverage:
    def test_every_declared_kind_has_a_fixture(self):
        # A new event kind must arrive with a round-trip test; this is what
        # makes that automatic rather than remembered.
        assert set(ALL_MAKERS) == set(EVENT_KINDS)


class TestRoundTrip:
    @pytest.mark.parametrize("kind", sorted(ALL_MAKERS))
    def test_append_then_read_returns_the_same_event(self, tmp_path, kind):
        path = get_lineage_path(str(tmp_path))
        written = append_event(path, ALL_MAKERS[kind]())

        events = read_events(path)

        assert events == [written]
        assert events[0].KIND == kind

    def test_append_stamps_id_and_timestamp(self, tmp_path):
        # Stamping is kind-independent (it lives on the shared event base), so
        # one kind exercises it for all of them.
        path = get_lineage_path(str(tmp_path))
        event = make_conversion()
        assert event.id is None
        assert event.recorded_at is None

        written = append_event(path, event)

        assert written.id
        assert written.recorded_at
        # The caller's own object is left untouched -- the stamp is on the copy.
        assert event.id is None
        assert event.recorded_at is None

    def test_caller_supplied_id_is_preserved(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        written = append_event(
            path, ConversionEvent(
                id="fixed-id",
                recorded_at="2026-01-02T03:04:05+00:00",
                direction="absorb",
                source=LineageEndpoint(repo="https://github.com/example/widget"),
                destination=LineageEndpoint(repo=".", path="packages/widget"),
                commit=SHA_A,
            ),
        )
        assert written.id == "fixed-id"
        assert read_events(path)[0].recorded_at == "2026-01-02T03:04:05+00:00"

    def test_related_to_links_events(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        conversion = append_event(path, make_conversion())
        tag_map = make_tag_map()
        tag_map.related_to = conversion.id
        append_event(path, tag_map)

        events = read_events(path)
        assert events[1].related_to == events[0].id

    def test_every_line_is_stamped_with_the_format_version(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        append_events(path, [m() for m in ALL_MAKERS.values()])

        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]

        assert len(lines) == len(ALL_MAKERS)
        for line in lines:
            assert line.startswith('{"format_version":1')
            assert json.loads(line)["format_version"] == CURRENT_FORMAT_VERSION

    def test_optional_fields_are_omitted_rather_than_written_null(self):
        data = json.loads(serialize_event(make_identity_transition()))
        assert "related_to" not in data
        data = json.loads(serialize_event(make_promotion_split_map()))
        assert "promoted_version" in data


class TestLocation:
    def test_standalone_repo_location(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        assert path == os.path.join(str(tmp_path), ".rlsbl", "lineage.jsonl")

    def test_releasable_location(self, tmp_path):
        releasable_dir = get_releasable_dir(str(tmp_path), "widget")
        path = get_lineage_path(str(tmp_path), releasable_dir=releasable_dir)
        assert path == os.path.join(
            str(tmp_path),
            ".rlsbl-monorepo",
            "releasables",
            "widget",
            "lineage.jsonl",
        )

    def test_standalone_location_round_trips(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        written = append_event(path, make_conversion())
        assert os.path.isfile(path)
        assert read_events(path) == [written]

    def test_both_locations_hold_the_same_format(self, tmp_path):
        standalone = tmp_path / "solo"
        workspace = tmp_path / "mono"
        solo_path = get_lineage_path(str(standalone))
        mono_path = get_lineage_path(
            str(workspace), releasable_dir=get_releasable_dir(str(workspace), "widget")
        )
        event = make_tag_map()
        event.id = "same"
        event.recorded_at = "2026-01-02T03:04:05+00:00"

        append_event(solo_path, event)
        append_event(mono_path, event)

        with open(solo_path, encoding="utf-8") as f:
            solo_bytes = f.read()
        with open(mono_path, encoding="utf-8") as f:
            mono_bytes = f.read()
        assert solo_bytes == mono_bytes
        assert read_events(solo_path) == read_events(mono_path)

    def test_append_creates_the_state_directory(self, tmp_path):
        releasable_dir = get_releasable_dir(str(tmp_path), "widget")
        assert not os.path.isdir(releasable_dir)
        path = get_lineage_path(str(tmp_path), releasable_dir=releasable_dir)

        append_event(path, make_boundary_alias())

        assert os.path.isdir(releasable_dir)

    def test_absent_record_reads_as_no_events(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        assert not lineage_file_exists(path)
        assert read_events(path) == []

    def test_exists_never_reads_content(self, tmp_path):
        # Detection must survive a record that read_events would reject: the
        # hard error belongs at the read-for-use site, not in scanning code.
        path = get_lineage_path(str(tmp_path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("this is not json at all\n")

        assert lineage_file_exists(path) is True
        with pytest.raises(LineageError):
            read_events(path)


class TestDurability:
    def test_separate_appends_accumulate_in_order(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        written = [append_event(path, ALL_MAKERS[k]()) for k in sorted(ALL_MAKERS)]

        assert read_events(path) == written

    def test_batch_append_preserves_order(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        batch = [make_conversion(), make_tag_map(), make_anchor_remap()]
        written = append_events(path, batch)

        assert [e.KIND for e in read_events(path)] == [
            "conversion",
            "tag-map",
            "anchor-remap",
        ]
        assert read_events(path) == written

    def test_append_never_rewrites_earlier_lines(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        append_event(path, make_conversion())
        with open(path, encoding="utf-8") as f:
            first = f.read()

        append_event(path, make_tag_map())
        with open(path, encoding="utf-8") as f:
            after = f.read()

        assert after.startswith(first)

    def test_empty_batch_writes_nothing(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        assert append_events(path, []) == []
        assert not os.path.exists(path)

    def test_invalid_event_in_a_batch_aborts_the_whole_append(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        append_event(path, make_conversion())
        with open(path, encoding="utf-8") as f:
            before = f.read()

        bad = make_tag_map()
        bad.mappings = []  # min_len 1
        with pytest.raises(LineageError):
            append_events(path, [make_anchor_remap(), bad])

        with open(path, encoding="utf-8") as f:
            assert f.read() == before

    def test_blank_lines_are_skipped(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        written = append_event(path, make_conversion())
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n   \n")

        assert read_events(path) == [written]

    def test_append_onto_a_file_missing_its_final_newline(self, tmp_path):
        # An interrupted write or a hand edit can leave the file without its
        # trailing newline. A blind append would concatenate the new event onto
        # that line and destroy BOTH.
        path = get_lineage_path(str(tmp_path))
        first = append_event(path, make_conversion())
        with open(path, encoding="utf-8") as f:
            body = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(body.rstrip("\n"))

        second = append_event(path, make_tag_map())

        assert read_events(path) == [first, second]

    def test_append_after_a_torn_last_line_isolates_the_new_event(self, tmp_path):
        # The damaged line stays damaged -- read_events will name it, which is
        # the point. What must not happen is the NEW event being swallowed by it.
        path = get_lineage_path(str(tmp_path))
        append_event(path, make_conversion())
        torn = '{"format_version":1,"kind":"tag-m'
        with open(path, "a", encoding="utf-8") as f:
            f.write(torn)

        written = append_event(path, make_tag_map())

        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        assert lines[1] == torn
        assert json.loads(lines[2])["id"] == written.id

    def test_kinds_filter_selects_without_relaxing_validation(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        append_events(path, [make_conversion(), make_tag_map(), make_anchor_remap()])

        selected = read_events(path, kinds=["tag-map"])
        assert [e.KIND for e in selected] == ["tag-map"]


def write_raw(path, *lines):
    """Write raw JSONL lines, bypassing serialization, and return the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def valid_payload():
    return {
        "format_version": 1,
        "kind": "conversion",
        "id": "e1",
        "recorded_at": "2026-01-02T03:04:05+00:00",
        "direction": "extract",
        "source": {"repo": "."},
        "destination": {"repo": "https://github.com/example/widget"},
        "commit": SHA_A,
    }


class TestMalformedRecordsAreHardErrors:
    """Each variant the brief names, plus the file/line siting of the message."""

    def _expect(self, tmp_path, payload_or_line, *, needle):
        path = get_lineage_path(str(tmp_path))
        line = (
            payload_or_line
            if isinstance(payload_or_line, str)
            else json.dumps(payload_or_line)
        )
        write_raw(path, line)
        with pytest.raises(LineageError) as exc:
            read_events(path)
        message = str(exc.value)
        assert path in message, message
        assert message.startswith(f"{path}:1: "), message
        assert needle in message, message
        return message

    def test_bad_json(self, tmp_path):
        self._expect(tmp_path, '{"format_version":1,"kind":', needle="malformed JSON")

    def test_not_an_object(self, tmp_path):
        self._expect(tmp_path, '["not", "an", "object"]', needle="JSON object")

    def test_unknown_kind(self, tmp_path):
        payload = valid_payload()
        payload["kind"] = "teleportation"
        self._expect(tmp_path, payload, needle="teleportation")

    def test_missing_kind(self, tmp_path):
        payload = valid_payload()
        del payload["kind"]
        self._expect(tmp_path, payload, needle="kind")

    def test_missing_required_field(self, tmp_path):
        payload = valid_payload()
        del payload["commit"]
        self._expect(tmp_path, payload, needle="commit")

    def test_missing_required_nested_field(self, tmp_path):
        payload = valid_payload()
        del payload["source"]["repo"]
        self._expect(tmp_path, payload, needle="repo")

    def test_missing_format_version(self, tmp_path):
        payload = valid_payload()
        del payload["format_version"]
        self._expect(tmp_path, payload, needle="format_version")

    def test_wrong_format_version(self, tmp_path):
        payload = valid_payload()
        payload["format_version"] = 2
        self._expect(tmp_path, payload, needle="format_version")

    def test_unknown_key(self, tmp_path):
        payload = valid_payload()
        payload["surprise"] = True
        self._expect(tmp_path, payload, needle="surprise")

    def test_bad_enum_value(self, tmp_path):
        payload = valid_payload()
        payload["direction"] = "sideways"
        self._expect(tmp_path, payload, needle="direction")

    def test_bad_sha_shape(self, tmp_path):
        payload = valid_payload()
        payload["commit"] = "not-a-sha"
        self._expect(tmp_path, payload, needle="commit")

    def test_wrong_field_type(self, tmp_path):
        payload = valid_payload()
        payload["direction"] = 7
        self._expect(tmp_path, payload, needle="direction")

    def test_empty_required_array(self, tmp_path):
        payload = {
            "format_version": 1,
            "kind": "tag-map",
            "id": "e1",
            "recorded_at": "2026-01-02T03:04:05+00:00",
            "mappings": [],
        }
        self._expect(tmp_path, payload, needle="mappings")

    def test_naive_timestamp_without_offset(self, tmp_path):
        payload = valid_payload()
        payload["recorded_at"] = "2026-01-02T03:04:05"
        self._expect(tmp_path, payload, needle="recorded_at")

    def test_error_names_the_offending_line_number(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        # Distinct ids: repeating one would trip the uniqueness check on line 2
        # before the read ever reached the malformed line this test is about.
        good = json.dumps(valid_payload())
        also_good = json.dumps({**valid_payload(), "id": "e2"})
        bad = json.dumps({**valid_payload(), "id": "e3", "kind": "teleportation"})
        write_raw(path, good, also_good, bad)

        with pytest.raises(LineageError) as exc:
            read_events(path)

        assert str(exc.value).startswith(f"{path}:3: ")

    def test_a_malformed_line_of_an_unwanted_kind_still_stops_the_read(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        good = json.dumps(valid_payload())
        bad = json.dumps({"format_version": 1, "kind": "tag-map", "id": "x"})
        write_raw(path, good, bad)

        with pytest.raises(LineageError):
            read_events(path, kinds=["conversion"])


class TestUndecodableBytes:
    """Byte corruption is a named record error, never a bare decode traceback."""

    def _write_bytes(self, tmp_path, data: bytes) -> str:
        path = get_lineage_path(str(tmp_path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_invalid_utf8_names_the_file_and_line(self, tmp_path):
        good = json.dumps(valid_payload()).encode("utf-8")
        path = self._write_bytes(
            tmp_path, good + b"\n" + b'{"format_version":1,"kind":"\xff\xfe"}\n'
        )

        with pytest.raises(LineageError) as exc:
            read_events(path)

        message = str(exc.value)
        assert message.startswith(f"{path}:2: "), message
        assert "UTF-8" in message, message

    def test_invalid_utf8_on_the_first_line(self, tmp_path):
        path = self._write_bytes(tmp_path, b"\xff\xfe not text at all\n")

        with pytest.raises(LineageError) as exc:
            read_events(path)

        assert str(exc.value).startswith(f"{path}:1: ")


class TestDuplicateIds:
    """The schema promises ids are unique within the file; the reader enforces it."""

    def test_duplicate_id_is_a_hard_error(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        line = json.dumps(valid_payload())
        write_raw(path, line, line)

        with pytest.raises(LineageError) as exc:
            read_events(path)

        message = str(exc.value)
        assert message.startswith(f"{path}:2: "), message
        assert "e1" in message, message
        assert "line 1" in message, message

    def test_duplicate_id_across_different_kinds_is_still_an_error(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        conversion = json.dumps(valid_payload())
        tag_map = json.dumps(
            {
                "format_version": 1,
                "kind": "tag-map",
                "id": "e1",
                "recorded_at": "2026-01-02T03:04:05+00:00",
                "mappings": [
                    {"old_tag": "widget@v0.3.0", "new_tag": "v0.3.0", "new_commit": SHA_B}
                ],
            }
        )
        write_raw(path, conversion, tag_map)

        with pytest.raises(LineageError) as exc:
            read_events(path)

        assert "e1" in str(exc.value)

    def test_duplicate_check_ignores_the_kinds_filter(self, tmp_path):
        # Same reason a malformed line of an unwanted kind stops the read: a
        # record that cannot be read in full cannot be trusted in part.
        path = get_lineage_path(str(tmp_path))
        line = json.dumps(valid_payload())
        write_raw(path, line, line)

        with pytest.raises(LineageError):
            read_events(path, kinds=["tag-map"])

    def test_distinct_ids_read_fine(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        first = json.dumps(valid_payload())
        second = json.dumps({**valid_payload(), "id": "e2"})
        write_raw(path, first, second)

        assert [e.id for e in read_events(path)] == ["e1", "e2"]


class TestCopySemantics:
    """The stamped return value is a TOP-LEVEL copy; nested values are shared.

    Pinned deliberately: the module documents exactly this, and deep-copying
    anchor-remap mapping lists (which can hold thousands of entries) is not
    worth the cost. The test exists so the docs cannot drift away from the code.
    """

    def test_stamp_does_not_touch_the_callers_object(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        event = make_conversion()

        written = append_event(path, event)

        assert written is not event
        assert event.id is None

    def test_nested_value_objects_are_shared_with_the_caller(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        event = make_conversion()

        written = append_event(path, event)

        assert written.source is event.source
        assert written.destination is event.destination

    def test_nested_mapping_lists_are_shared_with_the_caller(self, tmp_path):
        path = get_lineage_path(str(tmp_path))
        event = make_anchor_remap()

        written = append_event(path, event)

        assert written.mappings is event.mappings
        assert written.mappings[0] is event.mappings[0]


class TestModuleSurface:
    def test_kind_constants_match_the_declared_arm_set(self):
        assert set(EVENT_KINDS) == {
            lineage.KIND_CONVERSION,
            lineage.KIND_TAG_MAP,
            lineage.KIND_ANCHOR_REMAP,
            lineage.KIND_DEPARTED_GLOBS,
            lineage.KIND_BOUNDARY_ALIAS,
            lineage.KIND_IDENTITY_TRANSITION,
            lineage.KIND_PROMOTION_SPLIT_MAP,
        }

    def test_generated_validator_is_paired_with_the_runtime(self):
        import strictspec

        from rlsbl.strictspec_gen import lineage_event_validator as validator

        assert validator.GENERATED_BY == strictspec.__version__
        assert validator.SCHEMA_FORMAT_VERSION == CURRENT_FORMAT_VERSION
