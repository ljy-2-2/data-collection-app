# Teleoperation V1 integration and cleanup

Status: implemented (2026-09-01)

## What

- Problem / goal: make `projects/Teleoperation/` the clear V1 project entry while keeping the Unity headset app, PC runtime, and legacy reference code in separate maintainable directories; freeze the successfully accepted PICO + G2 direct-joint and video paths; remove only reproducible caches and superseded build artifacts.
- User-visible behavior: a new top-level `README.md` explains the complete architecture, directory ownership, one-command launch, APK location, safety controls, verification status, and the role of `test-teleop`; architecture status shows the verified `SessionSupervisor` path in green.
- Success criteria: source code and runtime behavior remain unchanged; the final APK and two final acceptance screenshots remain; Unity `Library/` and logs, Python bytecode caches, the rollback/acceptance APKs, and four intermediate screenshots are removed; V1 milestone, session plan, README files, and generated diagrams consistently say that the 60-second unified live acceptance passed; the complete PC runtime test suite remains green after cleanup.
- Out of scope: moving or merging Unity source into Python, importing `test-teleop` into the runtime, changing robot/video protocols, changing safety limits, rebuilding the APK, running another physical robot session, deleting Git metadata, deleting PICO SDK files, or deleting user recordings/data.

## How

- Approach: preserve three clear modules below `Teleoperation/`; add a top-level navigation document instead of physically mixing incompatible Unity and Python projects. Update existing status sources, regenerate their SVG/PNG outputs, and clean only an explicit audited list of generated artifacts.
- Components / files touched: `Teleoperation/README.md`; existing V1 milestone/session documents; `teleop-runtime/README.md`; architecture DOT/Mermaid sources and generated SVG/PNG; Unity `README.md`; audited generated/cache paths only.
- Phases: document and diagram freeze; exact cleanup; offline verification and size report.
- Risks / unknowns: deleting Unity `Library/` saves about 5 GB but the next Unity launch will reimport packages and rebuild the cache; removing rollback APKs removes local rollback convenience, while the final `Galaxis-VR-Teleop_1.1.6.apk` remains untouched.
- Safety boundary: no PICO/G2 connection or motion; no source, SDK, final APK, final acceptance screenshot, Git metadata, recording, or legacy reference code deletion. Destructive cleanup uses only exact audited paths.
- Verification plan: verify retained files before and after cleanup; run Bash syntax checks and all PC runtime tests; validate all architecture DOT sources and regenerate diagrams; scan documentation for stale pending/orange wording; report exact before/after sizes and every material deleted path.

## Result

- Added `Teleoperation/README.md` and the thin `Teleoperation/run_teleoperation.sh` delegating entry; Unity and Python remain separate modules and no runtime logic was duplicated.
- The root entry passed Bash syntax/help checks and refused before PICO/G2 work when `--enable-motion` was absent.
- The complete PC runtime suite passed `172/172`; no physical robot session was run during integration or cleanup.
- Regenerated and visually inspected the architecture status SVG/PNG. `SessionSupervisor` is green based on the operator-reported successful 60-second unified-entry acceptance; IK, locomotion, Leju, Recorder, and Monitor remain honestly orange/gray.
- Removed Unity `Library/` and logs, Python bytecode caches, two superseded APKs, and four intermediate screenshots after explicit confirmation. `Galaxis-VR-Teleop` decreased from about `5.4 GB` to `326 MB`; `teleop-runtime` is `1.7 MB` and `test-teleop` is `636 KB` after cache removal.
- Rechecked that the formal `Galaxis-VR-Teleop_1.1.6.apk`, both final acceptance screenshots, source directories, PICO SDK, documents, and legacy reference files remain present.
