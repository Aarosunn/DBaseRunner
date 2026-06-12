"""TDD tests for harness.py core logic: timed_call and run_sweep.

HTTP is mocked — no live servers needed. Tests cover:
  - timed_call: return shape, URL targeting, raise_for_status, error propagation
  - run_sweep: row counts, warmup flag, CSV schema, clear_fn protocol, trial_num sequence
  - CSV round-trip: data survives a real csv.DictWriter → DictReader cycle
"""

import csv
import io
import pytest
from unittest.mock import MagicMock, call

from harness import CSV_FIELDNAMES, run_sweep, timed_call


# ── helpers ──────────────────────────────────────────────────────────────────

def mock_session(content=b'{"ok": true}'):
    session = MagicMock()
    resp = MagicMock()
    resp.content = content
    session.post.return_value = resp
    return session, resp


def accumulating_writer():
    """Returns (writer, rows) where rows grows as writerow is called."""
    rows = []
    writer = MagicMock()
    writer.writerow.side_effect = rows.append
    return writer, rows


# ── timed_call ───────────────────────────────────────────────────────────────

class TestTimedCall:
    def test_returns_latency_and_response_bytes(self):
        session, _ = mock_session(content=b"hello world")  # 11 bytes
        latency_ms, response_bytes = timed_call(session, "http://x/ep", {"k": "v"})
        assert latency_ms >= 0
        assert response_bytes == 11

    def test_latency_is_milliseconds_not_seconds(self):
        session, _ = mock_session()
        latency_ms, _ = timed_call(session, "http://x/ep", {})
        # Mock call takes < 1ms; if we accidentally returned seconds it would be ~0.000001
        assert latency_ms < 5000

    def test_posts_to_correct_url_with_payload(self):
        session, _ = mock_session()
        timed_call(session, "http://svc/walker/load_own_tweets", {"user_id": "u1"})
        session.post.assert_called_once_with(
            "http://svc/walker/load_own_tweets", json={"user_id": "u1"}
        )

    def test_calls_raise_for_status(self):
        session, resp = mock_session()
        timed_call(session, "http://x/ep", {})
        resp.raise_for_status.assert_called_once()

    def test_http_error_propagates(self):
        session = MagicMock()
        resp = MagicMock()
        resp.content = b""
        resp.raise_for_status.side_effect = RuntimeError("HTTP 500")
        session.post.return_value = resp
        with pytest.raises(RuntimeError, match="HTTP 500"):
            timed_call(session, "http://x/ep", {})

    def test_empty_body_gives_zero_bytes(self):
        session, _ = mock_session(content=b"")
        _, response_bytes = timed_call(session, "http://x/ep", {})
        assert response_bytes == 0


# ── run_sweep row counts ──────────────────────────────────────────────────────

class TestRunSweepRowCounts:
    def test_single_param_value_produces_warmup_plus_trials(self):
        writer, rows = accumulating_writer()
        run_sweep("jac", "fanout", [500], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=20, trials=30, timestamp_fn=lambda: 0.0)
        assert len(rows) == 50

    def test_multiple_param_values_multiply_rows(self):
        writer, rows = accumulating_writer()
        run_sweep("postgres", "fanout", [100, 250, 500, 750, 1000],
                  MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=20, trials=30, timestamp_fn=lambda: 0.0)
        assert len(rows) == 250  # 5 param values × 50

    def test_warmup_row_count_per_param_value(self):
        writer, rows = accumulating_writer()
        run_sweep("jac", "selectivity", [50], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=20, trials=30, timestamp_fn=lambda: 0.0)
        assert sum(1 for r in rows if r["warmup"] == 1) == 20

    def test_timed_row_count_per_param_value(self):
        writer, rows = accumulating_writer()
        run_sweep("jac", "selectivity", [50], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=20, trials=30, timestamp_fn=lambda: 0.0)
        assert sum(1 for r in rows if r["warmup"] == 0) == 30

    def test_timed_fn_called_warmup_plus_trials_times(self):
        writer, _ = accumulating_writer()
        timed_fn = MagicMock(return_value=(10.0, 100))
        run_sweep("jac", "fanout", [500], timed_fn, writer,
                  warmup_count=20, trials=30, timestamp_fn=lambda: 0.0)
        assert timed_fn.call_count == 50


# ── run_sweep CSV schema ──────────────────────────────────────────────────────

class TestRunSweepCsvSchema:
    def _rows(self, backend="postgres", sweep_type="fanout", param_value=500,
               latency=42.5, resp_bytes=256, clear_fn=None):
        writer, rows = accumulating_writer()
        timed_fn = MagicMock(return_value=(latency, resp_bytes))
        run_sweep(backend, sweep_type, [param_value], timed_fn, writer,
                  warmup_count=1, trials=1, clear_fn=clear_fn, timestamp_fn=lambda: 1_700_000_000.0)
        return rows

    def test_all_csv_fieldnames_present_in_every_row(self):
        for row in self._rows():
            for field in CSV_FIELDNAMES:
                assert field in row, f"Missing field: {field}"

    def test_backend_field_set_correctly(self):
        rows = self._rows(backend="sqlalchemy")
        assert all(r["backend"] == "sqlalchemy" for r in rows)

    def test_sweep_type_field_set_correctly(self):
        rows = self._rows(sweep_type="selectivity")
        assert all(r["sweep_type"] == "selectivity" for r in rows)

    def test_param_value_field_set_correctly(self):
        rows = self._rows(param_value=750)
        assert all(r["param_value"] == 750 for r in rows)

    def test_latency_ms_field_set_correctly(self):
        rows = self._rows(latency=42.5)
        assert all(r["latency_ms"] == 42.5 for r in rows)

    def test_response_bytes_field_set_correctly(self):
        rows = self._rows(resp_bytes=256)
        assert all(r["response_bytes"] == 256 for r in rows)

    def test_timestamp_field_set_correctly(self):
        rows = self._rows()
        assert all(r["timestamp"] == 1_700_000_000.0 for r in rows)

    def test_warmup_flag_is_1_for_warmup_rows(self):
        rows = self._rows()
        warmup_rows = [r for r in rows if r["trial_num"] < 1 and r["warmup"] == 1]
        assert len(warmup_rows) >= 1

    def test_warmup_flag_is_0_for_timed_rows(self):
        writer, rows = accumulating_writer()
        timed_fn = MagicMock(return_value=(10.0, 100))
        run_sweep("neo4j", "fanout", [100], timed_fn, writer,
                  warmup_count=2, trials=3, timestamp_fn=lambda: 0.0)
        timed_rows = [r for r in rows if r["warmup"] == 0]
        assert len(timed_rows) == 3
        assert all(r["warmup"] == 0 for r in timed_rows)

    def test_timed_fn_receives_param_value_as_argument(self):
        writer, _ = accumulating_writer()
        timed_fn = MagicMock(return_value=(10.0, 100))
        run_sweep("jac", "fanout", [750], timed_fn, writer,
                  warmup_count=1, trials=1, timestamp_fn=lambda: 0.0)
        assert all(c == call(750) for c in timed_fn.call_args_list)


# ── run_sweep trial_num sequence ──────────────────────────────────────────────

class TestRunSweepTrialNum:
    def test_warmup_trial_num_is_sequential_from_zero(self):
        writer, rows = accumulating_writer()
        run_sweep("jac", "fanout", [500], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=5, trials=3, timestamp_fn=lambda: 0.0)
        warmup_nums = [r["trial_num"] for r in rows if r["warmup"] == 1]
        assert warmup_nums == list(range(5))

    def test_timed_trial_num_is_sequential_from_zero(self):
        writer, rows = accumulating_writer()
        run_sweep("jac", "fanout", [500], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=3, trials=5, timestamp_fn=lambda: 0.0)
        timed_nums = [r["trial_num"] for r in rows if r["warmup"] == 0]
        assert timed_nums == list(range(5))

    def test_trial_num_resets_per_param_value(self):
        writer, rows = accumulating_writer()
        run_sweep("jac", "fanout", [100, 500], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=2, trials=3, timestamp_fn=lambda: 0.0)
        # Each param_value should have timed trial_nums 0,1,2 — not 0,1,2,3,4,5
        timed_rows = [r for r in rows if r["warmup"] == 0]
        first_block = [r["trial_num"] for r in timed_rows[:3]]
        second_block = [r["trial_num"] for r in timed_rows[3:]]
        assert first_block == [0, 1, 2]
        assert second_block == [0, 1, 2]


# ── run_sweep clear_fn (Jac L1 cache reset) ───────────────────────────────────

class TestRunSweepClearFn:
    def test_clear_fn_called_once_per_timed_trial(self):
        writer, _ = accumulating_writer()
        clear_fn = MagicMock()
        run_sweep("jac", "fanout", [500], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=20, trials=30, clear_fn=clear_fn, timestamp_fn=lambda: 0.0)
        assert clear_fn.call_count == 30

    def test_clear_fn_not_called_during_warmup(self):
        """clear_fn fires only in timed trials; all clear calls happen after all warmup rows."""
        writer, rows = accumulating_writer()
        clear_call_row_counts = []

        def tracking_clear():
            clear_call_row_counts.append(len(rows))

        run_sweep("jac", "fanout", [500], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=20, trials=5, clear_fn=tracking_clear, timestamp_fn=lambda: 0.0)

        # Every clear call happened after all 20 warmup rows were written
        assert all(n >= 20 for n in clear_call_row_counts)

    def test_none_clear_fn_does_not_raise(self):
        writer, rows = accumulating_writer()
        run_sweep("postgres", "fanout", [500], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=5, trials=5, clear_fn=None, timestamp_fn=lambda: 0.0)
        assert len(rows) == 10

    def test_clear_fn_called_per_param_value_not_total(self):
        """3 param values × 4 trials = 12 clear calls total."""
        writer, _ = accumulating_writer()
        clear_fn = MagicMock()
        run_sweep("jac", "fanout", [100, 500, 1000], MagicMock(return_value=(10.0, 100)), writer,
                  warmup_count=2, trials=4, clear_fn=clear_fn, timestamp_fn=lambda: 0.0)
        assert clear_fn.call_count == 12


# ── CSV round-trip ────────────────────────────────────────────────────────────

class TestCsvRoundTrip:
    def test_rows_survive_dictwriter_dictreader_cycle(self):
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        timed_fn = MagicMock(return_value=(15.5, 512))
        run_sweep("neo4j", "selectivity", [25], timed_fn, writer,
                  warmup_count=2, trials=3, timestamp_fn=lambda: 1_000.0)
        buf.seek(0)
        rows = list(csv.DictReader(buf))
        assert len(rows) == 5
        assert sum(1 for r in rows if r["warmup"] == "0") == 3
        assert sum(1 for r in rows if r["warmup"] == "1") == 2
        assert all(r["latency_ms"] == "15.5" for r in rows)
        assert all(r["response_bytes"] == "512" for r in rows)
        assert all(r["backend"] == "neo4j" for r in rows)
