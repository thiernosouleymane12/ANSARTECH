"""Point d'entrée de ANSARTECH 224.

En local :      python app.py
Sur Render :    gunicorn app:app
"""
import os

from ansartech import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
