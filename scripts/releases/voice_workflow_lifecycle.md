The workflow lifecycle capability already exists in canonical source. The running
voice image predates it and carries separate sandbox/repair changes in the same
route. `voice_workflow_lifecycle.diff` applies only that existing capability:

- authenticated workflow capabilities read;
- optional active/deactivated initial status (active remains the default);
- status passed through to the existing workflow model before its first insert.

Apply the diff only to the reviewed source in the current running image. Build
FROM that exact immutable image, verify the overlay diff and all other Python
files, and retain Compose settings. This is an image build input, not an
instruction to patch the dirty production checkout. No migration is needed.
The create-route tests cover status validation and actual DB persistence plus
the existing activation gate. Verify a disposable candidate is deactivated
before testing any calling path. Do not activate it or connect to a provider.
