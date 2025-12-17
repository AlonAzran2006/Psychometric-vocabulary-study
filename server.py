# -*- coding: utf-8 -*-
"""
שרת FastAPI מרובה משתמשים (Multi-Tenant) ללימוד אוצר מילים, עובד עם Firebase Firestore.
- טעינת נתונים גלובליים (המילים עצמן) ל-RAM ב-startup.
- טעינת ציונים ואימונים ל-RAM פר-משתמש (Session-based) על פי ה-user_uid.
- שמירה אסינכרונית ל-Firestore לנתיבים פרטיים (users/{user_uid}/...).
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import deque
from typing import List, Dict, Any, Optional, Tuple, Literal

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

# 🌟 ייבוא Firebase
import firebase_admin
from firebase_admin import credentials, firestore

# =========================
#        תצורה ונתיבים
# =========================

APP_NAME = "WordsTrainer"

# 🌟 משתני סביבה ל-Firebase (במקום Cloudflare)
FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_CREDENTIALS")

# 🌟 משתנה גלובלי ל-Firestore Client
db = None

# תיקיות מקומיות
DATA_DIR = "data"


# ============================================
#  Firestore Functions - מעודכן ל-Multi-Tenant
# ============================================

# === Firestore Grades – users/<user_uid>/grades/<word_id> ===

# 🌟 הפונקציה מקבלת user_uid
def fs_set_grade(user_uid: str, word_id: str, grade: float) -> None:
    """
    שומר ציון למילה אחת ב-Firestore תחת ה-UID של המשתמש.
    """
    if db is None:
        return

    try:
        # 🌟 הנתיב החדש: users/{user_uid}/grades/{word_id}
        db.collection('users').document(user_uid).collection('grades').document(word_id).set({
            "grade": float(grade),
            "last_updated": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print(f"[FS] Failed to set grade for {word_id} for user {user_uid}: {e}")


# === Firestore Trainings – users/<user_uid>/trainings/<training_name> ===

# 🌟 הפונקציה מקבלת user_uid
def fs_set_training(user_uid: str, training_name: str, payload_data: Dict[str, Any]) -> None:
    """שומר מסמך אימון פרטי ב-Firestore (יצירה ודריסה)."""
    if db is None:
        return
    try:
        # 🌟 הנתיב החדש: users/{user_uid}/trainings/{training_name}
        db.collection('users').document(user_uid).collection('trainings').document(training_name).set(payload_data)
    except Exception as e:
        print(f"[FS] Failed to set training {training_name} for user {user_uid}: {e}")


# 🌟 הפונקציה מקבלת user_uid
def fs_get_training(user_uid: str, training_name: str) -> Optional[Dict[str, Any]]:
    """מחזיר את נתוני האימון המלאים (כמילון) או None עבור משתמש ספציפי."""
    if db is None:
        return None
    try:
        # 🌟 הנתיב החדש
        doc = db.collection('users').document(user_uid).collection('trainings').document(training_name).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"[FS] Failed to get training {training_name} for user {user_uid}: {e}")
        return None


# 🌟 הפונקציה מקבלת user_uid
def fs_list_training_names(user_uid: str) -> List[str]:
    """מחזיר רשימת שמות אימונים פרטיים למשתמש."""
    if db is None:
        return []
    try:
        # 🌟 הנתיב החדש
        return [doc.id for doc in db.collection('users').document(user_uid).collection('trainings').stream()]
    except Exception as e:
        print(f"[FS] Failed to list training names for user {user_uid}: {e}")
        return []


# 🌟 הפונקציה מקבלת user_uid
def fs_update_training_log(user_uid: str, training_name: str, word_id: str, action: str) -> None:
    """מעדכן את רשימות ה-removed_ids או added_to_end ב-Firestore עבור משתמש ספציפי."""
    if db is None:
        return

    # 🌟 קריאה לפונקציה המעודכנת
    data = fs_get_training(user_uid, training_name)
    if data is None:
        print(f"[FS] Training {training_name} not found for log update for user {user_uid}.")
        return

    # 1. מעדכן את ה-Log (לוגיקה נשארת זהה)
    removed_ids = data.get("removed_ids", [])
    added_to_end = data.get("added_to_end", [])

    if action == "removed":
        if word_id not in removed_ids:
            removed_ids.append(word_id)
        if word_id in added_to_end:
            added_to_end.remove(word_id)
    elif action == "added":
        if word_id not in added_to_end:
            added_to_end.append(word_id)
        if word_id in removed_ids:
            removed_ids.remove(word_id)

    data["removed_ids"] = removed_ids
    data["added_to_end"] = added_to_end

    # 2. שומר את המבנה המעודכן (דריסה מלאה של המסמך) באמצעות הפונקציה המעודכנת
    fs_set_training(user_uid, training_name, data)


# === Firestore Meta (אימון אחרון) – users/<user_uid>/meta/last_training ===

# 🌟 הפונקציה מקבלת user_uid
def fs_set_last_training(user_uid: str, training_name: str) -> None:
    """שומר ב-Firestore את שם האימון האחרון עבור משתמש ספציפי."""
    if db is None:
        return
    try:
        # 🌟 הנתיב החדש: users/{user_uid}/meta/last_training
        db.collection('users').document(user_uid).collection('meta').document('last_training').set(
            {"name": training_name})
    except Exception as e:
        print(f"[FS] Failed to set last training for user {user_uid}: {e}")


# 🌟 הפונקציה מקבלת user_uid
def fs_get_last_training(user_uid: str) -> Optional[str]:
    """מחזיר את שם האימון האחרון מ-Firestore עבור משתמש ספציפי."""
    if db is None:
        return None
    try:
        # 🌟 הנתיב החדש
        doc = db.collection('users').document(user_uid).collection('meta').document('last_training').get()
        data = doc.to_dict()
        return data.get('name') if doc.exists and data else None
    except Exception as e:
        print(f"[FS] Failed to get last training for user {user_uid}: {e}")
        return None


# ============================================

# =========================
#         מודלים ומשתנים גלובליים
# =========================

# ===== In-memory Global state (Words data only) =====
# 🌟 נתונים גלובליים בלבד (מילים גולמיות מ-JSON)
all_words_data_base: Dict[str, Dict[str, Any]] = {}
word_index: Dict[str, Tuple[Dict[str, Any], int]] = {}

# 🌟 חדש: מנהל מצב המשתמשים (Session Manager)
# { user_uid: { 'trainings': {...}, 'current_name': '...', 'queue': deque(...), 'user_grades': {...} } }
user_sessions: Dict[str, Dict[str, Any]] = {}


# ===== Request models =====

# 🌟 מודל בסיס חדש: כל בקשה חייבת לכלול user_uid
class UserRequestBase(BaseModel):
    user_uid: str = Field(..., description="Unique ID of the authenticated user (from Firebase Auth)")


# 🌟 כל המודלים יורשים כעת מ-UserRequestBase
class MemorizationUpdateWordRequest(UserRequestBase):
    word_id: str = Field(..., description="word id (e.g., w_11630)")
    new_grade: Literal[0, 5, 10] = Field(
        ..., description="New knowing grade: 0, 5, or 10"
    )


class MemorizeUnitRequest(UserRequestBase):
    file_index: int = Field(..., ge=1, le=10, description="1..10")


class UpdateKnowingGradeRequest(UserRequestBase):
    file_index: int = Field(..., ge=1, le=10, description="1..10")
    word_id: str = Field(..., description="word id (e.g., w_11630)")
    test_grade: int = Field(..., ge=-1, le=1, description="-1, 0, or 1")


class CreateTrainingRequest(UserRequestBase):
    training_name: str = Field(..., min_length=1)
    file_indexes: List[int] = Field(..., min_length=1)

    @validator("file_indexes", each_item=True)
    def validate_file_index(cls, v):
        if v < 1 or v > 10:
            raise ValueError("file_indexes must be 1..10")
        return v


class LoadTrainingRequest(UserRequestBase):
    training_name: str = Field(..., min_length=1)


# ============================================
#  Helpers & Session Management
# ============================================

# 🌟 פונקציה מעודכנת להשתמש במצב הגלובלי החדש
def build_words_index():
    """
    בונה מיפוי id -> (אובייקט מילה מלא, file_index שבו נמצאת).
    טוען את כל הנתונים הגולמיים מ-10 הקבצים ל-RAM (all_words_data_base).
    """
    global word_index, all_words_data_base

    word_index = {}
    all_words_data_base = {}

    for idx in range(1, 11):
        filename = os.path.join(DATA_DIR, f"words ({idx}).json")
        if not os.path.exists(filename):
            continue

        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            continue

        for item in data:
            wid = item.get("id")
            if wid:
                # 🌟 שמירת העתק נקי (ללא ציונים מהקובץ) בבסיס הנתונים הגלובלי
                item.pop("knowing_grade", None)
                word_index[wid] = (item, idx)
                all_words_data_base[wid] = item.copy()

    print(f"Word index built. {len(all_words_data_base)} unique words loaded to RAM.")


def calc_new_grade(old_grade: float, test_grade: int) -> float:
    if test_grade == -1:
        return round(old_grade * 0.5, 2)
    if test_grade == 0:
        return round(old_grade, 2)
    if test_grade == 1:
        return round((10 - old_grade) * 0.5 + old_grade, 2)
    raise HTTPException(status_code=400, detail="test_grade must be -1, 0, or 1")


def make_queue_item(word_obj: Dict[str, Any], file_index: int, grade: float) -> Dict[str, Any]:
    """בונה פריט לתור האימון."""
    meaning_value = word_obj.get("meaning") or word_obj.get("meaning_he") or ""
    return {
        "id": word_obj.get("id", ""),
        "word": word_obj.get("word", ""),
        "meaning": meaning_value,
        "file_index": file_index,
        "knowing_grade": round(grade, 2)  # 🌟 הוספת ציון המשתמש
    }


# 🌟 הפונקציה מקבלת את הציונים הפרטיים של המשתמש
def build_train_queue(training_ids: List[str], user_grades: Dict[str, float]) -> deque:
    # בניית התור לפי ה-IDs שיש בזיכרון
    new_queue = deque()
    missing_ids = []

    for wid in training_ids:
        entry = word_index.get(wid)
        if not entry:
            missing_ids.append(wid)
            continue

        wobj, fidx = entry
        # 🌟 קורא את הציון הפרטי של המשתמש
        grade = user_grades.get(wid, -1.0)
        new_queue.append(make_queue_item(wobj, fidx, grade))

    if missing_ids:
        print(f"Missing word IDs in word index: {missing_ids[:10]}")

    return new_queue


# לוגיקה זו נשארת גלובלית
def rebuild_queue_ids(original_ids: List[str], removed_ids: List[str], added_to_end: List[str]) -> List[str]:
    # ... לוגיקה נשארת זהה ...
    removed_set = set(removed_ids)

    # 1. סינון הרשימה המקורית: הסר מילים שכבר יצאו
    remaining_ids = [
        wid for wid in original_ids
        if wid not in removed_set
    ]

    # 2. הוספת מילים שהוכנסו מחדש (חיזוק) לסוף
    remaining_ids.extend(added_to_end)

    return remaining_ids


# 🌟 חדש: פונקציית ניהול סשנים
def get_user_session(user_uid: str) -> Dict[str, Any]:
    """
    מחזיר את אובייקט הסשן של המשתמש.
    אם הסשן לא קיים, הוא נוצר ומטענים הנתונים הפרטיים שלו מ-Firestore.
    """
    if user_uid in user_sessions:
        return user_sessions[user_uid]

    print(f"Initializing new session for user: {user_uid}")

    # 1. יצירת סשן בסיס חדש
    session = {
        'in_memory_trainings': {},  # שמות האימונים -> רשימת ID משוחזרת
        'current_training_name': None,
        'current_training_ids': [],
        'training_queue': deque(),
        'user_grades': {}  # word_id -> grade
    }
    user_sessions[user_uid] = session

    # 2. טעינת ציונים פרטיים (users/{user_uid}/grades)
    if db:
        try:
            grades_docs = db.collection('users').document(user_uid).collection('grades').stream()
            for doc in grades_docs:
                grade_data = doc.to_dict()
                grade_value = grade_data.get("grade")
                if grade_value is not None:
                    session['user_grades'][doc.id] = float(grade_value)
            print(f"Loaded {len(session['user_grades'])} grades for user {user_uid}.")
        except Exception as e:
            print(f"Error loading grades for user {user_uid}: {e}")

    # 3. טעינת אימונים פרטיים (users/{user_uid}/trainings)
    keys = fs_list_training_names(user_uid)  # 🌟 קורא את שמות האימונים הפרטיים
    for tname in keys:
        training_data = fs_get_training(user_uid, tname)  # 🌟 קורא את נתוני האימון הפרטיים
        if training_data:
            original_ids = training_data.get('original_ids', [])
            removed_ids = training_data.get('removed_ids', [])
            added_to_end = training_data.get('added_to_end', [])
            restored_ids = rebuild_queue_ids(original_ids, removed_ids, added_to_end)
            session['in_memory_trainings'][tname] = restored_ids

    # 4. טעינת האימון האחרון והפעלת התור הראשוני (users/{user_uid}/meta/last_training)
    last_name = fs_get_last_training(user_uid)
    if last_name and last_name in session['in_memory_trainings']:
        session['current_training_name'] = last_name
        session['current_training_ids'] = session['in_memory_trainings'][last_name][:]

        # 🌟 בניית התור באמצעות הציונים הפרטיים של המשתמש
        session['training_queue'] = build_train_queue(session['current_training_ids'], session['user_grades'])
        print(f"Loading last training: {last_name}. Queue size: {len(session['training_queue'])}")

    return session


# =========================
#        FastAPI אפליקציה
# =========================

app = FastAPI(title="Words Trainer Multi-Tenant API", version="1.0.0")

# CORS נשאר כפי שהוא
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "https://v0-vocabulary-learning-app-sigma.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.head("/health")
def health_check():
    """
    נקודת קצה פשוטה המשמשת לניטור (Ping) כדי למנוע הירדמות שרת.
    """
    # 🌟 אין צורך בבדיקה מורכבת של DB או RAM, רק החזרת סטטוס
    return {"status": "ok", "app": "WordsTrainer API"}

@app.get("/")
def root():
    return {"status": "ok"}


# --- on_startup (מצומצם) ---
@app.on_event("startup")
def on_startup():
    """
    בעת עליית השרת:
    1. אתחול Firebase.
    2. טעינת כל המילים מכל הקבצים ל-RAM (נתונים גלובליים בלבד).
    3. כל נתוני המשתמש נטענים רק בפנייה הראשונה לשרת (באמצעות get_user_session).
    """
    global db

    print("startup")

    # 1. אתחול Firebase Firestore
    if FIREBASE_CREDENTIALS_JSON:
        try:
            cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS_JSON))
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            print("Firebase Firestore initialized.")
        except Exception as e:
            print(f"Error initializing Firebase: {e}")
            db = None
    else:
        print("FIREBASE_CREDENTIALS_JSON not set. Running in local-only mode.")
        db = None

    # 2. טוען את כל המילים מכל הקבצים ל-RAM (גלובלי)
    build_words_index()  # ממלא את word_index ו-all_words_data_base

    # 🌟 אין יותר טעינה של grades, trainings, או last_training גלובליים!
    # 🌟 הכל מטופל על ידי user_sessions.


# --- memorization_update_word ---

@app.post("/memorization_update_word")
def memorization_update_word(req: MemorizationUpdateWordRequest, background_tasks: BackgroundTasks):
    """
    עדכון ציון מילה ישירות ל-0, 5, או 10. (פר-משתמש)
    """
    # 🌟 שליפת סשן המשתמש
    session = get_user_session(req.user_uid)

    # 1) קורא את נתוני המילה הגלובליים
    target_item_base = all_words_data_base.get(req.word_id)
    if target_item_base is None:
        raise HTTPException(
            status_code=404,
            detail=f"Word id {req.word_id} not found in global RAM index."
        )

    # 🌟 קורא את הציון הישן ממצב הזיכרון של המשתמש
    old_grade = session['user_grades'].get(req.word_id, 0.0)
    new_grade = float(req.new_grade)

    # 🌟 עדכון הציון במצב הזיכרון של המשתמש
    session['user_grades'][req.word_id] = new_grade

    # 🌟 שמירת הציון ל-Firestore ברקע (מעביר user_uid)
    background_tasks.add_task(fs_set_grade, req.user_uid, req.word_id, new_grade)

    return {
        "status": "ok",
        "updated": {
            "word_id": req.word_id,
            "old_knowing_grade": old_grade,
            "new_knowing_grade": new_grade
        }
    }


# --- memorize_unit ----

@app.post("/memorize_unit")
def memorize_unit(req: MemorizeUnitRequest):
    """
    מחזיר מילון של כל המילים ביחידה (file_index) כולל הציון הנוכחי שלהן (פר-משתמש).
    """
    # 🌟 שליפת סשן המשתמש
    session = get_user_session(req.user_uid)
    requested_file_index = req.file_index
    unit_words = []

    for wid, (wobj, fidx) in word_index.items():
        if fidx == requested_file_index:
            # 🌟 קורא את הציון המעודכן ישירות ממצב המשתמש
            current_grade = session['user_grades'].get(wid, -1.0)

            meaning_value = wobj.get("meaning") or wobj.get("meaning_he") or ""

            # 🌟 מעביר את הציון הפרטי ל-make_queue_item
            unit_words.append(make_queue_item(wobj, fidx, current_grade))

    if not unit_words:
        raise HTTPException(
            status_code=404,
            detail=f"No words found for file_index {requested_file_index}"
        )

    return {
        "status": "ok",
        "file_index": requested_file_index,
        "word_count": len(unit_words),
        "words": unit_words
    }


# --- update_knowing_grade ---

@app.post("/update_knowing_grade")
def update_knowing_grade(req: UpdateKnowingGradeRequest, background_tasks: BackgroundTasks):
    """
    מעדכן knowing_grade ומחזיר את המילה הבאה בתור (פר-משתמש).
    """
    # 🌟 שליפת סשן המשתמש והמשתנים הפרטיים
    session = get_user_session(req.user_uid)
    training_queue = session['training_queue']
    current_training_name = session['current_training_name']

    # 1) קורא את נתוני המילה הגלובליים
    target_item_base = all_words_data_base.get(req.word_id)
    if target_item_base is None:
        raise HTTPException(
            status_code=404,
            detail=f"Word id {req.word_id} not found in global RAM index."
        )

    # 🌟 קורא ציון ישן ממצב המשתמש
    old_grade = session['user_grades'].get(req.word_id, 0.0)
    new_grade = calc_new_grade(old_grade, req.test_grade)

    # 🌟 עדכון הציון במצב הזיכרון של המשתמש
    session['user_grades'][req.word_id] = new_grade

    # 🌟 שמירת הציון ל-Firestore ברקע (מעביר user_uid)
    background_tasks.add_task(fs_set_grade, req.user_uid, req.word_id, new_grade)

    # 2) חיזוק / עדכון Log האימון

    # 🌟 שימוש בפונקציות תור מקומיות
    if not training_queue:
        return {"status": "error", "detail": "No active training queue for this user."}

    training_queue.popleft()

    if current_training_name:
        # 🌟 שמירה ברקע של הסרת המילה (מעביר user_uid)
        background_tasks.add_task(fs_update_training_log, req.user_uid, current_training_name, req.word_id, "removed")

    if req.test_grade == -1:
        appears_again = any(node.get("id") == req.word_id for node in training_queue)

        if not appears_again:
            wobj, fidx = word_index.get(req.word_id, (None, None))
            if wobj and fidx:
                # 🌟 שימוש ב-grade המעודכן של המשתמש ליצירת פריט התור
                training_queue.append(make_queue_item(wobj, fidx, new_grade))

                if current_training_name:
                    # 🌟 שמירה ברקע של הוספת המילה ל-Log (מעביר user_uid)
                    background_tasks.add_task(fs_update_training_log, req.user_uid, current_training_name, req.word_id,
                                              "added")
            else:
                print(f"Could not find word {req.word_id} in word_index to re-add to queue.")

    # שליפת המילה הבאה
    next_item = training_queue[0] if training_queue else None

    if next_item is None:
        return {
            "status": "ok",
            "training_complete": True,
            "message": "No more words in the training queue."
        }

    return {
        "status": "ok",
        "training_complete": False,
        "next_word": next_item,
        "updated": {
            "word_id": req.word_id,
            "test_grade": req.test_grade,
            "old_knowing_grade": old_grade,
            "new_knowing_grade": new_grade
        }
    }


# --- create_training ---

@app.post("/create_training")
def create_training(req: CreateTrainingRequest):
    """
    יוצר רצף אימון (sequence) פרטי על בסיס נתוני המילים הגלובליים וציוני המשתמש.
    """
    # 🌟 שליפת סשן המשתמש
    session = get_user_session(req.user_uid)

    # 1. יצירת רשימה של כל ה-IDs שיש לכלול לפי file_indexes (לוגיקה גלובלית נשארת)
    eligible_word_ids = [wid for wid, (_, fidx) in word_index.items() if fidx in req.file_indexes]

    if not eligible_word_ids:
        raise HTTPException(
            status_code=400,
            detail=f"No words found for the specified file_indexes."
        )

    words_with_grades = []
    # 2. לולאה על המילים וסינון/חישוב
    for wid in eligible_word_ids:
        # 🌟 קורא ציון ממצב המשתמש
        grade = session['user_grades'].get(wid, -1.0)

        if 0 <= grade < 9.9:
            words_with_grades.append((wid, grade))

    if not words_with_grades:
        raise HTTPException(
            status_code=400,
            detail=f"No eligible words found for training '{req.training_name}'"
        )

    # 3. החלטת מספר הופעות ובניית רצף (Sequence) (לוגיקה נשארת)
    word_repeat = []
    for wid, g in words_with_grades:
        repeats = 3 if g < 5 else (2 if g <= 9 else 1)
        word_repeat.append((wid, repeats))

    sequence = [wid for wid, _ in word_repeat]
    sequence.extend([wid for wid, r in word_repeat if r >= 2])
    sequence.extend([wid for wid, r in word_repeat if r >= 3])
    random.shuffle(sequence)

    # 4. שמירת האימון ב-Firestore (מעביר user_uid)
    payload_data = {
        "original_ids": sequence,
        "removed_ids": [],
        "added_to_end": [],
    }
    fs_set_training(req.user_uid, req.training_name, payload_data)  # 🌟 פונקציה מעודכנת

    # 5. עדכון זיכרון הסשן
    session['in_memory_trainings'][req.training_name] = sequence[:]

    session['current_training_name'] = req.training_name
    session['current_training_ids'] = sequence[:]

    # 🌟 בניית תור באמצעות הציונים הפרטיים של המשתמש
    session['training_queue'] = build_train_queue(session['current_training_ids'], session['user_grades'])

    # 6. עדכון האימון האחרון ב-Firestore (מעביר user_uid)
    fs_set_last_training(req.user_uid, req.training_name)  # 🌟 פונקציה מעודכנת

    return {
        "status": "ok",
        "training_name": req.training_name,
        "num_unique_words": len(word_repeat),
        "total_items_in_sequence": len(sequence),
        "message": f"Training '{req.training_name}' created successfully."
    }


# --- load_training ---

@app.post("/load_training")
def load_training(req: LoadTrainingRequest):
    """
    טוען אימון לזיכרון (תור) פר-משתמש ומחזיר מיד את המילה הראשונה.
    """
    # 🌟 שליפת סשן המשתמש
    session = get_user_session(req.user_uid)
    requested_name = req.training_name

    # 1) אם זה האימון הנוכחי – אין צורך לטעון מחדש
    if session['current_training_name'] == requested_name and session['current_training_ids']:
        training_ids = session['current_training_ids'][:]
    else:
        # 2) קודם ננסה מהזיכרון הכללי של האימונים (כבר משוחזר)
        if requested_name in session['in_memory_trainings']:
            training_ids = session['in_memory_trainings'][requested_name][:]
        else:
            # 3) רק אם אין בזיכרון – נטען מ-Firestore ונשחזר (מעביר user_uid)
            training_data = fs_get_training(req.user_uid, requested_name)  # 🌟 פונקציה מעודכנת

            if training_data is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Training '{requested_name}' not found for user {req.user_uid}"
                )

            original_ids = training_data.get('original_ids', [])
            removed_ids = training_data.get('removed_ids', [])
            added_to_end = training_data.get('added_to_end', [])

            # שחזור התור
            training_ids = rebuild_queue_ids(original_ids, removed_ids, added_to_end)

            # שומרים את הרשימה המשוחזרת לזיכרון הסשן
            session['in_memory_trainings'][requested_name] = training_ids[:]

        # 4) עדכון "האימון הנוכחי" + שמירה כאימון אחרון
        session['current_training_name'] = requested_name
        session['current_training_ids'] = training_ids[:]
        fs_set_last_training(req.user_uid, requested_name)  # 🌟 פונקציה מעודכנת

        # 🌟 בניית תור באמצעות הציונים הפרטיים של המשתמש
        session['training_queue'] = build_train_queue(training_ids, session['user_grades'])

    if not isinstance(training_ids, list) or len(training_ids) == 0:
        return {
            "status": "ok",
            "training_name": requested_name,
            "training_complete": True,
            "queue_size_remaining": 0,
            "message": "Training exists but has no words."
        }

    # שליפת המילה הראשונה מהתור של המשתמש
    first_item = session['training_queue'].training_queue[0] if session['training_queue'] else None

    if first_item is None:
        return {
            "status": "ok",
            "training_name": requested_name,
            "training_complete": True,
            "queue_size_remaining": 0,
            "message": "No words left to practice in this training."
        }

    return {
        "status": "ok",
        "training_name": requested_name,
        "training_complete": False,
        "queue_size_remaining": len(session['training_queue']),
        "first_word": first_item
    }


# --- list_trainings ---

@app.get("/list_trainings")
# 🌟 מקבלת את ה-UID כפרמטר query (או header, תלוי איך הפרונט-אנד יעביר אותו)
# לצורך הפשטות, נשתמש ב-Header/Query parameter כרגע
def list_trainings(user_uid: str):
    """
    מחזיר רשימת אימונים מתוך הזיכרון (של המשתמש הנוכחי).
    """
    # 🌟 שליפת סשן המשתמש
    session = get_user_session(user_uid)
    trainings = []

    # 🌟 קריאה למצב הזיכרון הפרטי של המשתמש
    for tname, ids in session['in_memory_trainings'].items():
        trainings.append({
            "name": tname,
            "word_count": len(ids),
            "source": "Firestore"
        })
    return {"status": "ok", "trainings": trainings}


# =========================
#        הפעלה מקומית
# =========================

if __name__ == "__main__":
    import uvicorn

    file_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(file_dir)

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)