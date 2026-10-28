# Performance checks

Run the same benchmark script against each checkout under one Python environment. Install the checked-in dependencies first with `uv sync --frozen`. Both checkouts must have identical `uv.lock` files. The script records interpreter, installed dependency fingerprint, lock hash, commit, dirty state, fixture hash, geometry, CPU count and load for each round. Environment recording includes only locale, timezone and terminal settings.

```sh
uv run python scripts/benchmark.py --smoke --sizes 100 --output .agent-work/benchmark-smoke
uv run python scripts/benchmark.py \
  --checkout /path/to/baseline --checkout /path/to/candidate \
  --samples 5 --output .agent-work/benchmark-comparison
```

Output must be inside a Git-ignored directory. The script verifies the ignore rule before writing. Each worker runs in a fresh process using the launcher's Python environment and imports the target checkout's `src/`. It never installs dependencies or contacts a mail server. Keep unrelated builds and tests idle during timing. Inspect the recorded load before interpreting results. Five rounds run in baseline, candidate, baseline, candidate order. Smoke mode performs one round for correctness only.

`benchmark.json` contains raw samples, per-scenario medians and ranges, failure counts and unsupported capabilities. Individual round JSON and stderr files preserve failures even if a worker crashes. Failed correctness checks produce a nonzero exit and do not enter timing summaries. Every scenario records elapsed wall time, process CPU time, completed work and available protocol counters. `ms` always uses milliseconds.

## Timed paths

| Scenario | Boundary and correctness gate |
| --- | --- |
| `headers.first_page` | App test startup through 100 populated table rows and a completed viewport refresh. Checks exact descending UIDs, total, table subject, visible subject text, and exactly 100 fetched headers. A 45-line viewport cannot display all 100 rows simultaneously. |
| `headers.next_page` | Older-page action through 200 rows and refresh, with exact order and 100 new headers. Runs for sizes above 100. |
| `fuzzy.extract` | Searchable mapping construction and RapidFuzz extraction, with fixed top UID or no-match expectation. |
| `fuzzy.input_to_frame` | Input value change, application debounce, scoring, table construction and viewport refresh. |
| `fuzzy.render` | Scoring, table construction and refresh with the current query. Compare with extraction and the full input path. |
| `mime.parse` | Parse fixed plain, HTML, nested multipart and oversized messages. Verify marker text, removed scripts, attachment names and truncation. |
| `body.render` | Existing Markdown body through completed viewport refresh. Verify Markdown content, visible marker text, and rendered block count. |
| `body.cold_to_frame` | Real backend fetch, MIME parsing and rendering. Requires one transport body fetch. |
| `body.cached_to_frame` | Same body through rendering. Requires zero additional transport commands. |
| `body.cache_eviction` | 25 distinct bodies followed by a refetch of the first. Checks the application cache eviction contract. |
| `network.navigation_to_frame` | Three real down-arrow key events through cursor refresh during a body request. Records whether the request remained pending, final selection, final preview correctness, and heartbeat gaps until the preview settles. |

Default header sizes are 100, 1,000 and 10,000. Fixed queries cover exact subject, typo, sender, diacritics and no-match results. The full table is not injected into the rendered screen before filtering, so large cases measure filtered rendering without an unrelated 10,000-row initial render. Fixture creation and header preloading happen outside fuzzy measurements. Empty-query behavior and broad-result tie ordering need separate ranking tests.

`--delay-ms 0 50 300` sets body delays in milliseconds. A heartbeat requests a tick every 5 ms. Its maximum observed gap includes event-loop scheduling and benchmark driver work. Textual's key driver waits for idle and completed animations after each key. These waits contribute to the measured input latency. A short simulated request may finish before the third key; inspect both pending flags. Heartbeat and CPU numbers alone do not identify a bottleneck.

These are synthetic IMAPClient responses through actual application paging, parsing and cache code, plus Textual's headless renderer. Protocol commands and payload bytes are counted in the in-process transport. They are not wire traffic. TLS, socket behavior, a real terminal emulator and external delivery need separate verification. Run the local TLS protocol tests for protocol correctness.

## Profiling and interpretation

Run profiles separately from timed rounds:

```sh
uv run python scripts/benchmark.py --profile --sizes 1000 \
  --checkout /path/to/candidate --output .agent-work/benchmark-profile
uv run python -m pstats .agent-work/benchmark-profile/profile-0.prof
```

Profile mode writes `cProfile` data and omits timing summaries. It profiles the event loop, renderer and benchmark driver on the main thread. Worker-thread MIME parsing needs a separate targeted profile, such as a `cProfile` wrapper around `parse_body`, or a sampling profiler that includes all threads. Do not use profiler timings as benchmark samples.

The report deliberately returns an inconclusive verdict. Before making a performance claim, verify equal configuration and dependencies, positive work, zero correctness failures, load, variation and a separately identified limiter. A difference within the observed spread establishes no measurable difference. Synthetic results cannot establish live-server performance.

## New capabilities

Account and conversation scenarios run only when the target exposes `AccountSession`. They have no initial-release baseline. Common paths retain the same raw fixtures for both targets. The report records separate hashes for common fixtures and conversation headers.

| Scenario | Boundary and correctness gate |
| --- | --- |
| `account.switch_to_frame` | Account-switch handler through the new mailbox frame. Two sessions share UIDs but have distinct addresses and subjects. Checks selected UID, exact page order, reset search and pagination, active address, and visible subject. |
| `account.slow_isolation_to_frame` | Switch and preview completion while the previous account has a blocked body fetch. Requires the new account to load and render before releasing the old transport. After release, checks that the old result cannot replace the new preview. |
| `conversation.group` | Group parsed headers through exact UID coverage, group count, and bounded group membership checks. |
| `conversation.group_to_frame` | Toggle grouping through the completed frame. Checks collapsed row count, loaded-member count, unread count, and visible conversation text. |
| `conversation.expand_to_frame` | Expand the first conversation through its completed frame. Checks every expanded UID, row count, selected identity, and visible conversation text. |
| `conversation.filter_to_frame` | Input, debounce, fuzzy matching, grouping, and completed frame. A unique match belongs to a nonrepresentative member. Checks that member's UID, selection, full conversation count, and visible match text. |

Each dataset has two independent account transports. The slow-account fixture uses a release gate rather than a fixed sleep. Its elapsed time measures the new account's rendered response while the old request remains blocked. The post-release stale-result check runs outside that timing boundary and can still fail the sample.

Conversation fixtures link ten consecutive messages through `References`. Every loaded UID appears once. A partial final conversation is allowed for custom dataset sizes. Parsing and fixture construction occur before conversation measurements. The unfiltered table setup also occurs outside grouping and expansion measurements.

Rendered-output checks read Textual's composed viewport after its refresh callback. Exact table values verify content that the terminal may truncate. These checks cover the headless application frame, not a terminal emulator's paint latency.
