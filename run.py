"""Development entry point. Use a production WSGI server to deploy."""
import os
from app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes"))
