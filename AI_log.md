# AI Log

In accordance with the assessment instructions, this document logs all generative AI assistance utilized during the completion of the City of Cape Town Data Science Unit Code Challenge.

---

## Log Entries

### Entry 1: Repository Scoping & Assessment Setup
* **Date**: 2026-11-06
* **Model**: Gemini 3.8 Flash (via Google Antigravity)
* **Goal**: Understand repository structure, filter requirements strictly for the Data Engineering track, clarify submission and fork requirements, and establish the AI documentation strategy.
* **Prompt Summary**: Focus only on sections related to Data Engineering, provide a brief about the repository, clarify forking requirements and AI logging guidelines.
* **Approximate Tokens**: ~3,000 input / ~1,200 output

#### Summary of AI Output:
* Extracted and highlighted only the relevant Data Engineering tasks:
  * **Section 0**: Setup and AWS S3 public data assets.
  * **Section 1**: S3 Select extraction of H3 resolution 8 data and schema conformance scoring.
  * **Section 2**: Initial spatial transformation (joining service requests with H3 res 8 grid) and error threshold design.
  * **Section 5**: Atlantis spatial subsampling, unreliable wind data ingestion/resilience strategy, and data anonymisation with human review provisioning.
* Clarified the intent and structure of `AI_log.md`.

#### Human Review & Decisions:
* Confirmed strict focus on Data Engineering tracks (bypassing Data Science, Analyst, and Front-End tracks).
* Established workflow: all git commits will be managed and executed by Farhaan Daniels directly. This creates master review process, no changes gets to repo if not reviewed.
* Standardized log naming as `AI_log.md` titled `# AI Log`.

### Entry 2: Task 1 (S3 Select extraction and conformance scoring)
* **Date**: 2026-11-06
* **Model**: Claude Sonnet 5.5 (via Claude Code)
* **Branch**: `main` (nothing committed or pushed by the AI)
* **Goal**: Implement Task 1: read H3 resolution 8 from `city-hex-polygons-8-10.geojson` with S3 Select, validate against `city-hex-polygons-8.geojson`, add a schema conformance score, log timings.
* **Prompt Summary**: Follow the README instructions, no commits, no pushes. Task 1 chosen for the first branch.
* **Tokens** (from the local session log, including repository setup): 12,607 input, 39,881 output, 156,060 cache read, 4,531,888 cache write.

#### Summary of AI Output:
* `src/extract.py`, `src/conformance.py`, `src/run_extraction.py`, `src/aws_client.py`, `src/timing.py`
* `config/hex_schema.yaml` (standalone schema and thresholds), `docs/task1_data_extraction.md`
* `tests/test_extraction.py` (14 unit tests plus one opt-in live test)

#### Human Review & Decisions:
* Chose Task 1 as the first task.
* **Instance where the AI output was corrected**: the first approach used S3 Select's JSON input and failed with `OverMaxRecordSize` (the file is one 108 MB document). The AI switched to line-oriented text input. A later live run then scored 0.94 (FAIL) because S3 Select's output CSV was quoting the header lines; the AI diagnosed it from the raw returned lines and set the output quote character, after which the score was 1.0000.
* Reviewed by Farhaan Daniels: the 0.99 / 0.95 threshold choice and rule weights in `config/hex_schema.yaml`.
* Farhaan Daniels reviewed and manually made changes where relevant.

### Entry 3: Task 2 (join service requests to H3 res 8 hexagons)
* **Date**: 2026-11-06
* **Model**: Claude Sonnet 5.5 (via Claude Code)
* **Branch**: `main` (nothing committed or pushed by the AI)
* **Goal**: Implement Task 2: assign each service request one H3 res 8 index, `0` for missing coordinates, join error threshold with motivation, logging, timings, validate against `sr_hex.csv.gz`.
* **Prompt Summary**: "Task 2: Join", continuing the workflow.
* **Tokens** (from the local session log): 547 input, 30,336 output, 0 cache read, 4,112,128 cache write.

#### Summary of AI Output:
* `src/sr_join.py`, `tests/test_sr_join.py` (20 tests), `docs/task2_sr_hex_join.md`; `requirements.txt` gains `h3` and `pandas`.
* Compared two join methods before choosing: H3 cell lookup (0 disagreements with the reference) and a shapely polygon join (29 disagreements with the reference, see `docs/task2_sr_hex_join.md`).

#### Human Review & Decisions:
* **Instances where the AI output was corrected by checking its own work**: (1) the first output had an extra `Unnamed: 0` column the reference lacks, which the AI only found by reading the written file back; (2) after fixing that, a full-column diff showed 2,726 changed cells (dropped leading zeros in `notification_number`, `.0` added to `reference_number`, `None` turned blank), so the CSV is now read as text. The output is now identical to the reference except 3 rows.
* Reviewed by Farhaan Daniels: the 0.1% threshold (a judgement call), and whether `0` is the right output for the 3 requests whose reference cell is outside the City grid.

### Entry 4: Task 5 first version (Atlantis subsample, wind and anonymisation)
* **Date**: 2026-11-07
* **Model**: Claude Sonnet 5.5 (via Claude Code)
* **Branch**: `main` (nothing committed or pushed by the AI)
* **Goal**: Implement Task 5: subsample around the Atlantis centroid, attach wind data with a strategy for the unreliable endpoint, anonymise, and hold records for manual review.
* **Prompt Summary**: "Task 5: Atlantis", continuing workflow.
* **Tokens** (from the local session log): 594 input, 49,638 output, 0 cache read, 4,249,462 cache write.

#### Summary of AI Output:
* `src/atlantis_subsample.py`, `src/http_fetch.py`, `src/wind.py`, `src/anonymise.py`, `src/run_atlantis.py`.
* This version had no tests and no write-up, and the wind parser was written against a guessed layout because the wind link returned 404. Entry 5 covers what was found wrong with it.

#### Human Review & Decisions:
* Reviewed by Farhaan Daniels

### Entry 5: finishing Task 5
* **Dates**: 2026-11-06 to 2026-11-07
* **Models**: Claude Opus 5.5 (via Claude Code)
* **Branch**: `main` (nothing committed or pushed by the AI)
* **Goal**: Compare the repository to the README, and build what was missing.
* **Prompt Summary**: "check the history of what was done", "what should we still build according to the readme", "Fix suburb first", "use ATLANTIS" and "do Batch 5: anonymisation tests and the wind-value privacy fix". Each batch was reported back before the next was asked for.
* **Tokens** (from the local session log, up to the start of this entry):
  * Claude Haiku 5.5: 142 input, 36,426 output, 8,712,431 cache read, 161,434 cache write.
  * Claude Opus 5.5: 202 input, 151,163 output, 22,512,549 cache read, 843,858 cache write.

#### Summary of AI Output:
* Tests for all Task 5 code: `tests/test_http_fetch.py`, `test_atlantis_subsample.py`, `test_wind.py`, `test_anonymise.py`, `test_run_all.py`.
* `docs/task5_atlantis.md`, `src/run_all.py`, and the "Submission: Data Engineering" section at the top of `README.md`.
* Found the wind data on the City's open data portal after the README link returned 404, and rewrote the parser for the real layout.

#### Human Review & Decisions:
* **Instance where Farhaan Daniels corrected the AI**: the README asks for an official suburb, so the AI recommended and switched to ATLANTIS INDUSTRIAL from the official planning layer. Farhaan Daniels rejected this because it is an industrial area and not a suburb, and directed the use of ATLANTIS. The AI's recommendation had also cut the sample from 10,292 to 444 requests. `docs/task5_atlantis.md` records the decision.

* **Faults in the AI's own Task 5 code, found by the tests and measurements added in Entry 5**:
  * Pipeline: `requirements.txt` was missing packages, so Task 5 could not run from a clean install.
  * `http_fetch.py`: a connection dropped mid-download crashed the run instead of retrying.
  * `atlantis_subsample.py`: some malformed server responses crashed the run instead of retrying.
  * `wind.py`: a file with several sites silently gave the wrong site's wind, and day-first dates were misread. The AI's first fix for the dates then swapped ISO dates, which a test caught before commit.
  * `anonymise.py`: exact wind values could reveal the time of a request despite the 6-hour bucket, `official_suburb` could narrow location below the hexagon, and rare cause codes were not checked.
* Reviewed by Farhaan Daniels: the list of sensitive categories in `src/anonymise.py`, the "low risk rather than risk free" conclusion in `docs/task5_atlantis.md`, and the assumptions there about wind timestamps.

### Entry 6: Closing the gaps
* **Date**: 2026-11-07
* **Model**: Claude Opus 5.5 (via Claude Code)
* **Branch**: `main` (nothing committed or pushed by the AI)
* **Goal**: Close every gap found when comparing the repository to the README.
* **Prompt Summary**: "did we cover everything in the readme"
* **Tokens**: included in Claude Opus 5.5's session total, which when the prompts were exported was 272 input, 222,419 output, 35,808,202 cache read and 943,226 cache write. 

#### Summary of AI Output:
* Applied `ruff` formatting and lint rules (`pyproject.toml`), and moved all imports to the top of their files.
* Pinned the tested package versions in `requirements.txt`.
* `tests/test_task_runners.py`: end-to-end tests of all three tasks. Line coverage of `src/` rose from 82% to 100%, with two unreachable defensive lines excluded.

#### Human Review & Decisions:
* **Reviewed tests**: `sr_join.download` saved a truncated download as if complete, and that file would then have been reused on every run. It now checks the size and retries.
* Reviewed by Farhaan Daniels

### Entry 6a: Shapely call and ISO8601 timestamp parsing
* **Date**: 2026-11-07
* **Model**: Claude Opus 5.5 (via Claude Code)
* **Branch**: `main` (nothing committed or pushed by the AI)
* **Goal**: Replace a deprecated shapely call and parse request timestamps as ISO8601, so that no timestamp is silently blanked.

#### Summary of AI Output:
* `src/atlantis_subsample.py`: `compute_centroid` now calls `shapely.transform` in place of the deprecated `shapely.ops.transform`.
* `src/anonymise.py`: `floor_to_bucket` parses request times with `format="ISO8601"`. Without it, pandas guessed one format from the first value and blanked any value in another format.
* `src/wind.py`: `attach_wind` parses `creation_timestamp` with `format="ISO8601"`.

### Entry 7: Changing the Task 5 suburb to an official one
* **Date**: 2026-11-08
* **Model**: Claude Haiku 5.5, then Claude Opus 5.5 (via Claude Code)
* **Branch**: `main` (nothing committed or pushed by the AI)
* **Goal**: Settle which suburb Task 5 should use, after Farhaan Daniels questioned the earlier choice of ATLANTIS.
* **Prompt Summary**: "is it supposed to be ATLANTIS INDUSTRIAL",  "check the official ones", then "must be careful, what is the official avondale" .

#### Summary of AI Output:
* Queried the official GIS layers and measured the requests around each official suburb near Atlantis.
* Changed `src/atlantis_subsample.py` to `AVONDALE - WESFLEUR` from the February 2019 official layer, with tests that reject `AVONDALE - PAROW` and a partial name match.
* Reran Task 5: the subsample went from 10,292 to 12,916 requests. Updated the affected documentation. This replaces the suburb decision in Entry 5.

#### Human Review & Decisions:
* **Instance where Farhaan Daniels corrected the AI**: Challenged the AI's statement that the ATLANTIS polygon was separate from the industrial area. The official data showed ATLANTIS INDUSTRIAL lies entirely inside it.
* **Instance where Farhaan Daniels corrected the AI**: Pointed out that there is more than one Avondale. The official data has `AVONDALE - WESFLEUR` and `AVONDALE - PAROW`, and the 2017 layer names both `AVONDALE`, so the code uses the 2019 layer and the exact name.
* Farhaan Daniels made the final decision to switch. `docs/task5_atlantis.md` records the reasoning.
* The AI's recommendation rests on reading "an official suburb in the proximity of Atlantis" as allowing any official suburb near Atlantis. The brief does not name one.

### Entry 8: Final check against the README
* **Date**: 2026-11-09
* **Model**: Claude Haiku 5.5, then Claude Opus 5.5 (via Claude Code)
* **Branch**: `main` (nothing committed or pushed by the AI)
* **Goal**: Check the submission against the README, then bring the README up to date.
* **Prompt Summary**: "do a last check", "dont commit or change anything", "make sure the README is the most up to date"

#### Summary of AI Output:
* Read-only review of the Data Engineering sections against the code and docs.
* `README.md`: replaced "under a minute" with the run times measured in CI, replaced the "not yet tested" note with what CI covers, and replaced a Windows-only test command with the `--test` / `-Test` option.
* Proposed updates to this log, for review.

#### Human Review & Decisions:
* **Instance where Farhaan Daniels corrected the AI**: the AI listed the Task 1 thresholds and the Task 5 cross-check as open actions. Farhaan Daniels had already reviewed the thresholds, and the cross-check is not required by the brief and is already disclosed in `docs/task5_atlantis.md`.