import os
import sys

import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider == "minimax":
        if not os.getenv("MINIMAX_API_KEY"):
            print("Error: MINIMAX_API_KEY environment variable is not set.", file=sys.stderr)
            sys.exit(1)
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("Error: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
            sys.exit(1)

    uvicorn.run(
        "web.app:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
        log_level="info",
    )
