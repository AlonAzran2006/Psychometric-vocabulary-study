# השתמש בתמונת בסיס רשמית של Python
FROM python:3.11-slim

# הגדרת משתנה סביבה כדי לא להשתמש בבאפר לפלט
ENV PYTHONUNBUFFERED 1

# הגדרת תיקיית העבודה בתוך הקונטיינר
WORKDIR /app

# העתק את קובץ הדרישות והתקן את הספריות
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# העתק את שאר הקוד והנתונים
# 🌟 ודא שיש לך תיקיית 'data' (עם קבצי ה-JSON של המילים)
COPY . /app

# הפעלת השרת באמצעות Gunicorn (מומלץ ב-Cloud Run ליישומי Python)
# Gunicorn יריץ את האפליקציה שלך (שנקראת 'app' בקובץ 'server') בפורט המוגדר ע"י Cloud Run.
# Gunicorn יעזור גם לביצועים.

# 🌟 המודול 'server' והמשתנה 'app' (מוגדר ב-FastAPI)
CMD exec gunicorn --bind :$PORT --workers 1 --worker-class uvicorn.workers.UvicornWorker server:app