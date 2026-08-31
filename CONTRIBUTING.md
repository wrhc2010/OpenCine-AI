# Contributing

## Local setup

Use Python 3.11+ and Node 18+. From `E:\AI-Video-Director` (or the equivalent
checkout path):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,api]"
pytest -q
ruff check src tests
cd web
npm install
npm run build
```

## Change expectations

- Keep Creative IR and provider contracts backwards-compatible.
- Add a contract test for every new provider and a recovery test for every new
  queue or state transition.
- Preserve fail-closed judging and bounded retry policy.
- Do not add code copied from an unlicensed repository. Record third-party
  licenses and notices for MIT/Apache-2.0 dependencies.
- Keep UI commands accessible by keyboard and expose evidence/reasoning in the
  operator control room.

Pull requests should explain the state transitions, persistence implications,
cost behavior and test fixture used.
