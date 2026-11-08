import os
from core import app, init_db
import auth, masters, inventory, sales, reports, treasury, accounting, cleanup  # noqa: F401  (register routes)

init_db()

if __name__ == "__main__":
    app.run(host=os.environ.get("AVAND_HOST", "127.0.0.1"), port=int(os.environ.get("AVAND_PORT", "5050")), debug=False)
