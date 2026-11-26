import signal
import sys

if __name__ == "__main__":
    # PID 1 ignores a SIGTERM it has no handler for (BUG-021); uvicorn replaces this one before it
    # imports the app, and calls it again on its way out.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
