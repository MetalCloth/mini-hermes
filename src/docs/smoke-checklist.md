# Oryn manual smoke checklist

Run `./oryn dashboard` with a valid Codex login and the React UI built (`cd web && npm run build`). Use a disposable project folder for write checks.

1. **Plain reply:** ask a simple question. Confirm the streamed text finishes and the message remains after reload.
2. **Read and multi-tool turn:** ask Oryn to inspect two named files. Confirm each tool starts and returns a result, then Oryn answers from both.
3. **Approved write:** ask it to create a disposable file, approve the preview, and confirm the file contents match.
4. **Denied write:** ask it to replace that file, deny the preview, and confirm the old contents remain and Oryn reports the denial.
5. **Stop while waiting for approval:** request another disposable write, press Stop at the approval prompt, then confirm no write occurred and the turn is marked stopped.
6. **Stop during a streamed reply:** use a request that takes long enough to stream, press Stop, reload the chat, and confirm the visible partial text is marked stopped. Ask Oryn to continue and confirm it receives the partial text plus the interruption note.
7. **Provider failure:** temporarily disconnect the network or use a request that returns an endpoint error. Confirm any streamed partial text is kept and marked failed, then retry in a new turn after connectivity returns.

Automated coverage for tool ordering, tool failures, approval denial/cancellation, provider errors, and partial-turn persistence lives in `src/tests` and runs with `python -m unittest discover -s src/tests`.
