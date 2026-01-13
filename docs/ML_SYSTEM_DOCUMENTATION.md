# ML СИСТЕМА BETTING BOT - ПОЛНАЯ ДОКУМЕНТАЦИЯ

> Техническая документация для переноса ML системы на другой проект

---

## ОГЛАВЛЕНИЕ

1. [Обзор архитектуры](#1-обзор-архитектуры)
2. [База данных - таблицы ML](#2-база-данных---таблицы-ml)
3. [Признаки (Features)](#3-признаки-features)
4. [Модели и обучение](#4-модели-и-обучение)
5. [Предсказания](#5-предсказания)
6. [Система калибровки](#6-система-калибровки)
7. [ROI-аналитика и самообучение](#7-roi-аналитика-и-самообучение)
8. [Интеграция компонентов](#8-интеграция-компонентов)
9. [Конфигурация](#9-конфигурация)
10. [Известные проблемы и TODO](#10-известные-проблемы-и-todo)

---

## 1. ОБЗОР АРХИТЕКТУРЫ

### 1.1 Общая схема

```
┌─────────────────────────────────────────────────────────────────────┐
│                         BETTING BOT ML SYSTEM                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐           │
│  │   СБОР      │───▶│  ОБУЧЕНИЕ   │───▶│ ПРЕДСКАЗАНИЕ │           │
│  │   ДАННЫХ    │    │   МОДЕЛЕЙ   │    │              │           │
│  └──────────────┘    └──────────────┘    └──────────────┘           │
│         │                   │                   │                    │
│         ▼                   ▼                   ▼                    │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐           │
│  │ ml_training  │    │ ml_models/   │    │  КАЛИБРОВКА  │           │
│  │ _data        │    │ ensemble_    │    │              │           │
│  │              │    │ models       │    │              │           │
│  └──────────────┘    └──────────────┘    └──────────────┘           │
│         │                                       │                    │
│         ▼                                       ▼                    │
│  ┌──────────────┐                       ┌──────────────┐            │
│  │ ВЕРИФИКАЦИЯ │──────────────────────▶│ ROI LEARNING │            │
│  │ РЕЗУЛЬТАТОВ │                        │              │            │
│  └──────────────┘                       └──────────────┘            │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 Жизненный цикл предсказания

```
1. Пользователь запрашивает анализ матча
2. Система собирает данные (форма, таблица, коэффициенты, xG, травмы...)
3. Данные преобразуются в вектор признаков (93 признака)
4. Ансамбль из 3 моделей делает предсказание
5. Применяется калибровка уверенности
6. Применяются ROI-корректировки
7. Формируется итоговая рекомендация
8. Предсказание сохраняется в БД
9. После матча - верификация результата
10. Обновление калибровки и ROI-статистики
11. Периодическое переобучение моделей
```

### 1.3 Зависимости

```python
# Обязательные библиотеки ML
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
import numpy as np
import joblib  # для сохранения моделей

# Опционально
import json
import sqlite3
import os
import logging
```

---

## 2. БАЗА ДАННЫХ - ТАБЛИЦЫ ML

### 2.1 ml_training_data - Основное хранилище данных обучения

```sql
CREATE TABLE IF NOT EXISTS ml_training_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER,              -- Связь с predictions
    bet_category TEXT,                  -- Категория ставки (outcomes_home, totals_over, btts...)
    features_json TEXT,                 -- JSON с признаками
    target INTEGER,                     -- Результат: 1=правильно, 0=неправильно, NULL=не проверено
    bet_rank INTEGER DEFAULT 1,         -- 1=MAIN ставка, 2+=ALT ставки
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE INDEX idx_ml_training_category ON ml_training_data(bet_category);
CREATE INDEX idx_ml_training_target ON ml_training_data(target);
```

**Категории ставок:**
- `outcomes_home` - Победа хозяев (П1)
- `outcomes_away` - Победа гостей (П2)
- `outcomes_draw` - Ничья (X)
- `totals_over` - Тотал больше 2.5 (ТБ)
- `totals_under` - Тотал меньше 2.5 (ТМ)
- `btts` - Обе забьют (ОЗ)
- `double_chance` - Двойной шанс
- `handicap` - Фора

### 2.2 ml_models - Метаданные одиночных моделей

```sql
CREATE TABLE IF NOT EXISTS ml_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type TEXT,                    -- Тип модели (gradient_boost, random_forest...)
    accuracy REAL,                      -- Точность на тестовой выборке
    precision_score REAL,               -- Precision
    recall_score REAL,                  -- Recall
    f1_score REAL,                      -- F1-score
    samples_count INTEGER,              -- Количество образцов обучения
    model_path TEXT,                    -- Путь к файлу модели
    trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 2.3 ensemble_models - Метаданные ансамблевых моделей

```sql
CREATE TABLE IF NOT EXISTS ensemble_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT,                    -- random_forest, gradient_boost, logistic
    model_type TEXT,                    -- Тип sklearn класса
    bet_category TEXT,                  -- Категория ставки
    accuracy REAL,
    precision_val REAL,
    recall_val REAL,
    f1_score REAL,
    samples_count INTEGER,
    feature_importance TEXT,            -- JSON с важностью признаков
    model_path TEXT,
    trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_name, bet_category)
);
```

### 2.4 confidence_calibration - Калибровка уверенности

```sql
CREATE TABLE IF NOT EXISTS confidence_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_category TEXT,                  -- Категория ставки
    confidence_band TEXT,               -- Диапазон: "30-40", "40-50", "50-60"...
    predicted_count INTEGER DEFAULT 0,  -- Сколько предсказаний в этом диапазоне
    actual_wins INTEGER DEFAULT 0,      -- Сколько из них выиграли
    calibration_factor REAL DEFAULT 1.0,-- Множитель коррекции
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bet_category, confidence_band)
);
```

**Логика калибровки:**
- Если предсказания с 70% уверенностью выигрывают только 55% - снизить уверенность
- calibration_factor = actual_wins / predicted_count
- Ограничен диапазоном 0.65-1.35

### 2.5 roi_analytics - ROI аналитика

```sql
CREATE TABLE IF NOT EXISTS roi_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_category TEXT,                  -- Категория ставки
    condition_key TEXT,                 -- "overall" или условие ("high_injuries", "away_favorite"...)
    total_bets INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_staked REAL DEFAULT 0,        -- Общая сумма ставок
    total_returned REAL DEFAULT 0,      -- Общий возврат
    roi_percent REAL DEFAULT 0,         -- ROI в процентах
    avg_odds REAL DEFAULT 0,            -- Средний коэффициент
    avg_ev REAL DEFAULT 0,              -- Средний EV
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bet_category, condition_key)
);
```

### 2.6 learning_patterns - Выученные паттерны

```sql
CREATE TABLE IF NOT EXISTS learning_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT,                  -- "success" или "failure"
    pattern_key TEXT,                   -- Уникальный ключ паттерна
    bet_category TEXT,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_predictions INTEGER DEFAULT 0,
    avg_confidence REAL DEFAULT 0,
    description TEXT,                   -- Описание паттерна
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pattern_type, pattern_key, bet_category)
);
```

### 2.7 feature_error_patterns - Паттерны ошибок

```sql
CREATE TABLE IF NOT EXISTS feature_error_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_category TEXT,
    condition_key TEXT,                 -- "high_injuries&away_favorite"
    total_predictions INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    avg_confidence_when_failed REAL,
    suggested_adjustment INTEGER,       -- На сколько корректировать уверенность
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bet_category, condition_key)
);
```

### 2.8 league_learning - Обучение по лигам

```sql
CREATE TABLE IF NOT EXISTS league_learning (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_code TEXT,                   -- "EPL", "LALIGA", "BUNDESLIGA"...
    bet_category TEXT,
    total_predictions INTEGER DEFAULT 0,
    correct_predictions INTEGER DEFAULT 0,
    accuracy REAL DEFAULT 0,
    avg_confidence REAL DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(league_code, bet_category)
);
```

### 2.9 learning_log - Журнал событий обучения

```sql
CREATE TABLE IF NOT EXISTS learning_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,                    -- "model_trained", "calibration_updated", "pattern_detected"
    description TEXT,
    data_json TEXT,                     -- Дополнительные данные в JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 3. ПРИЗНАКИ (FEATURES)

### 3.1 Полный список признаков (ML_FEATURE_COLUMNS)

```python
ML_FEATURE_COLUMNS = {
    # ===== ФОРМА КОМАНД (16 признаков) =====
    "home_wins": 0,                 # Побед хозяев за последние N матчей
    "home_draws": 0,                # Ничьих хозяев
    "home_losses": 0,               # Поражений хозяев
    "home_goals_scored": 1.5,       # Среднее забитых голов хозяев
    "home_goals_conceded": 1.0,     # Среднее пропущенных голов хозяев
    "home_home_win_rate": 50,       # % побед хозяев дома
    "home_btts_pct": 50,            # % матчей хозяев с BTTS
    "home_over25_pct": 50,          # % матчей хозяев с Over 2.5
    "away_wins": 0,                 # Побед гостей
    "away_draws": 0,                # Ничьих гостей
    "away_losses": 0,               # Поражений гостей
    "away_goals_scored": 1.0,       # Среднее забитых голов гостей
    "away_goals_conceded": 1.5,     # Среднее пропущенных голов гостей
    "away_away_win_rate": 30,       # % побед гостей в гостях
    "away_btts_pct": 50,            # % матчей гостей с BTTS
    "away_over25_pct": 50,          # % матчей гостей с Over 2.5

    # ===== ТУРНИРНАЯ ТАБЛИЦА (3 признака) =====
    "home_position": 10,            # Позиция хозяев в таблице
    "away_position": 10,            # Позиция гостей в таблице
    "position_diff": 0,             # Разница позиций (home - away)

    # ===== КОЭФФИЦИЕНТЫ (9 признаков) =====
    "odds_home": 2.5,               # Коэффициент на победу хозяев
    "odds_draw": 3.5,               # Коэффициент на ничью
    "odds_away": 3.0,               # Коэффициент на победу гостей
    "implied_home": 0.4,            # Подразумеваемая вероятность П1
    "implied_draw": 0.25,           # Подразумеваемая вероятность X
    "implied_away": 0.35,           # Подразумеваемая вероятность П2

    # ===== H2H - ЛИЧНЫЕ ВСТРЕЧИ (4 признака) =====
    "h2h_home_wins": 0,             # Побед хозяев в личных встречах
    "h2h_draws": 0,                 # Ничьих в личных встречах
    "h2h_away_wins": 0,             # Побед гостей в личных встречах
    "h2h_total": 0,                 # Всего личных встреч

    # ===== EXPECTED GOALS - базовые (5 признаков) =====
    "expected_goals": 2.5,          # Ожидаемый тотал матча
    "expected_home_goals": 1.3,     # Ожидаемые голы хозяев
    "expected_away_goals": 1.0,     # Ожидаемые голы гостей
    "expected_goals_method": 0,     # 1=home/away specific, 0=overall
    "avg_btts_pct": 50,             # Средний % BTTS обеих команд
    "avg_over25_pct": 50,           # Средний % Over 2.5 обеих команд

    # ===== СУДЕЙСКИЙ ФАКТОР (5 признаков) =====
    "referee_cards_per_game": 4.0,      # Карточек за игру у судьи
    "referee_penalties_per_game": 0.32, # Пенальти за игру
    "referee_reds_per_game": 0.12,      # Красных карточек за игру
    "referee_style": 2,                 # 4=очень строгий, 3=строгий, 2=сбалансированный, 1=мягкий
    "referee_cards_vs_avg": 0,          # Отклонение от среднего

    # ===== ЗАГРУЖЕННОСТЬ КАЛЕНДАРЯ (5 признаков) =====
    "home_rest_days": 5,            # Дней отдыха у хозяев
    "away_rest_days": 5,            # Дней отдыха у гостей
    "home_congestion_score": 0,     # 0=свежие, 1=норма, 2=уставшие, 3=измотаны
    "away_congestion_score": 0,
    "rest_advantage": 0,            # Положительное = хозяева отдохнули больше

    # ===== МОТИВАЦИЯ (7 признаков) =====
    "is_derby": 0,                  # 1 если дерби
    "home_motivation": 5,           # Мотивация хозяев (1-10)
    "away_motivation": 5,           # Мотивация гостей (1-10)
    "home_relegation_battle": 0,    # 1 если борьба за выживание
    "away_relegation_battle": 0,
    "home_title_race": 0,           # 1 если борьба за титул
    "away_title_race": 0,
    "motivation_diff": 0,           # home_motivation - away_motivation

    # ===== КЛАСС КОМАНДЫ (6 признаков) =====
    "home_is_elite": 0,             # 1 если топ-клуб (Real, Barca, Bayern...)
    "away_is_elite": 0,
    "home_team_class": 2,           # 4=elite, 3=strong, 2=midtable, 1=weak, 0=relegation
    "away_team_class": 2,
    "class_diff": 0,                # Положительное = хозяева сильнее
    "elite_vs_underdog": 0,         # 1 если элита играет со слабым
    "class_mismatch": 0,            # Абсолютная разница классов

    # ===== ДВИЖЕНИЕ ЛИНИЙ / SHARP MONEY (7 признаков) =====
    "home_odds_dropped": 0,         # 1 если коэфф на хозяев упал (sharp money)
    "away_odds_dropped": 0,         # 1 если коэфф на гостей упал
    "draw_odds_dropped": 0,         # 1 если коэфф на ничью упал
    "over_odds_dropped": 0,         # 1 если коэфф на тотал больше упал
    "under_odds_dropped": 0,        # 1 если коэфф на тотал меньше упал
    "sharp_money_detected": 0,      # 1 если любое значительное движение
    "line_movement_direction": 0,   # -1=гости, 0=стабильно, 1=хозяева

    # ===== СМЕНА ТРЕНЕРА (4 признака) =====
    "home_new_coach": 0,            # 1 если новый тренер (<5 матчей)
    "away_new_coach": 0,
    "home_coach_boost": 0,          # 0-15 буст от нового тренера
    "away_coach_boost": 0,

    # ===== ТРАВМЫ И СОСТАВЫ (8 признаков) =====
    "home_injuries": 0,             # Количество травмированных
    "away_injuries": 0,
    "total_injuries": 0,
    "home_lineup_confirmed": 0,     # 1 если состав известен
    "away_lineup_confirmed": 0,
    "home_injury_crisis": 0,        # 1 если 6+ травм
    "away_injury_crisis": 0,
    "fatigue_risk": 0,              # 1 если любая команда устала

    # ===== xG ПРОДВИНУТЫЕ (21 признак) =====
    "home_xg_per_game": 1.3,        # xG хозяев за игру
    "away_xg_per_game": 1.0,        # xG гостей за игру
    "home_xga_per_game": 1.0,       # xGA хозяев (пропущенный xG)
    "away_xga_per_game": 1.3,       # xGA гостей
    "home_xg_diff": 0,              # xG - реальные голы (>0 = недобирают)
    "away_xg_diff": 0,
    "total_xg_deviation": 0,        # Общее отклонение xG
    "xg_expected_total": 2.5,       # Ожидаемый тотал по xG
    "xg_expected_home": 1.3,        # Ожидаемые голы хозяев по xG
    "xg_expected_away": 1.0,        # Ожидаемые голы гостей по xG
    "home_recent_xg": 1.3,          # xG за последние 5 матчей
    "away_recent_xg": 1.0,
    "recent_xg_total": 2.3,         # Сумма recent xG
    "home_unlucky": 0,              # 1 если xG_diff > 2 (должны забивать больше)
    "away_unlucky": 0,
    "home_lucky": 0,                # 1 если xG_diff < -2 (перевыполняют)
    "away_lucky": 0,
    "both_underperforming": 0,      # 1 если total xG deviation > 3
    "both_overperforming": 0,
    "xg_data_available": 0,         # 1 если данные xG доступны

    # ===== ВЛИЯНИЕ ИГРОКОВ (14 признаков) =====
    "home_attack_modifier": 0,      # Отрицательное = атака слабее
    "away_attack_modifier": 0,
    "home_defense_modifier": 0,     # Отрицательное = защита слабее
    "away_defense_modifier": 0,
    "home_goals_modifier": 0,       # Прямое влияние на голы
    "away_goals_modifier": 0,
    "home_total_impact": 0,         # Общее влияние на команду %
    "away_total_impact": 0,
    "home_key_players_out": 0,      # Количество травмированных ключевых
    "away_key_players_out": 0,
    "home_star_out": 0,             # 1 если attack_modifier < -25%
    "away_star_out": 0,
    "home_defense_crisis": 0,       # 1 если defense_modifier < -25%
    "away_defense_crisis": 0,
    "player_impact_available": 0,   # 1 если данные доступны

    # ===== FLAT TRACK BULLY (6 признаков) =====
    "home_scoring_ratio": 1.0,          # bottom6/top6 scoring (>2 = flat track)
    "away_scoring_ratio": 1.0,
    "home_has_flat_track_bully": 0,     # 1 если есть flat track игроки
    "away_has_flat_track_bully": 0,
    "home_has_big_game_player": 0,      # 1 если есть big game игроки
    "away_has_big_game_player": 0,
    "home_scoring_adjustment": 0,       # Корректировка ожидаемых голов
    "away_scoring_adjustment": 0,
    "flat_track_available": 0,          # 1 если данные доступны

    # ===== ДОПОЛНИТЕЛЬНО =====
    "has_web_news": 0,              # 1 если есть новости из веба
}
```

### 3.2 Функция извлечения признаков

```python
def extract_features(home_form: dict, away_form: dict, standings: dict,
                     odds: dict, h2h: list, home_team: str, away_team: str,
                     referee_stats: dict = None, has_web_news: bool = False,
                     congestion: dict = None, motivation: dict = None,
                     team_class: dict = None, coach_factor: dict = None,
                     line_movement: dict = None, injuries: dict = None,
                     xg_data: dict = None, player_impact: dict = None,
                     flat_track_data: dict = None) -> dict:
    """
    Извлекает все признаки для ML модели.

    Возвращает словарь с ключами из ML_FEATURE_COLUMNS.
    Отсутствующие данные заполняются значениями по умолчанию.
    """
    # Начинаем с дефолтных значений
    features = dict(ML_FEATURE_COLUMNS)

    # Заполняем признаки из входных данных...
    # (см. полную реализацию в bot_secure.py:6185-6596)

    return features
```

### 3.3 Преобразование в вектор

```python
def features_to_vector(features: dict) -> list:
    """
    Преобразует словарь признаков в вектор для ML модели.

    ВАЖНО: Порядок признаков должен совпадать с обучением!
    Используем ML_FEATURE_COLUMNS.keys() для гарантии порядка.
    """
    return [
        features.get(name, default)
        for name, default in ML_FEATURE_COLUMNS.items()
    ]
```

---

## 4. МОДЕЛИ И ОБУЧЕНИЕ

### 4.1 Конфигурация ансамбля

```python
ENSEMBLE_MODEL_TYPES = {
    "random_forest": {
        "class": "RandomForestClassifier",
        "params": {
            "n_estimators": 100,      # Количество деревьев
            "max_depth": 10,          # Максимальная глубина
            "min_samples_split": 5,   # Минимум образцов для разделения
            "random_state": 42        # Для воспроизводимости
        },
        "weight": 1.0                 # Вес в голосовании
    },
    "gradient_boost": {
        "class": "GradientBoostingClassifier",
        "params": {
            "n_estimators": 100,
            "max_depth": 5,
            "learning_rate": 0.1,
            "random_state": 42
        },
        "weight": 1.2                 # Обычно лучше работает
    },
    "logistic": {
        "class": "LogisticRegression",
        "params": {
            "max_iter": 1000,
            "random_state": 42
        },
        "weight": 0.8                 # Простая базовая модель
    }
}
```

### 4.2 Функция обучения ансамбля

```python
def train_ensemble_models(bet_category: str) -> dict:
    """
    Обучает ансамбль моделей для конкретной категории ставок.

    Args:
        bet_category: одна из ["outcomes_home", "outcomes_away", "outcomes_draw",
                              "totals_over", "totals_under", "btts"]

    Returns:
        dict с результатами обучения каждой модели
    """
    if not ML_AVAILABLE:
        return {"error": "ML libraries not available"}

    # 1. Загрузка данных обучения
    X, y = get_ml_training_data(bet_category)

    if X is None or len(X) < ML_MIN_SAMPLES:  # ML_MIN_SAMPLES = 50
        return {"error": f"Not enough data: {len(X) if X else 0} < {ML_MIN_SAMPLES}"}

    # 2. Разделение на train/test
    X_train, X_test, y_train, y_test = train_test_split(
        np.array(X), np.array(y),
        test_size=0.2,
        random_state=42
    )

    feature_names = list(ML_FEATURE_COLUMNS.keys())
    results = {}

    # 3. Обучение каждой модели
    model_classes = {
        "random_forest": RandomForestClassifier(**ENSEMBLE_MODEL_TYPES["random_forest"]["params"]),
        "gradient_boost": GradientBoostingClassifier(**ENSEMBLE_MODEL_TYPES["gradient_boost"]["params"]),
        "logistic": LogisticRegression(**ENSEMBLE_MODEL_TYPES["logistic"]["params"]),
    }

    for model_name, model in model_classes.items():
        try:
            # Обучение
            model.fit(X_train, y_train)

            # Оценка
            y_pred = model.predict(X_test)
            accuracy = accuracy_score(y_test, y_pred)
            precision = precision_score(y_test, y_pred, average='weighted', zero_division=0)
            recall = recall_score(y_test, y_pred, average='weighted', zero_division=0)
            f1 = f1_score(y_test, y_pred, average='weighted', zero_division=0)

            # Важность признаков (для tree-based моделей)
            if hasattr(model, 'feature_importances_'):
                importance = dict(zip(feature_names, model.feature_importances_.tolist()))
                importance = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True)[:10])
            else:
                importance = {}

            # Сохранение модели
            model_path = f"{ML_MODELS_DIR}/{model_name}_{bet_category}.joblib"
            os.makedirs(ML_MODELS_DIR, exist_ok=True)
            joblib.dump({"model": model, "feature_names": feature_names}, model_path)

            # Сохранение в БД
            save_ensemble_model(
                model_name=model_name,
                model_type=ENSEMBLE_MODEL_TYPES[model_name]["class"],
                bet_category=bet_category,
                accuracy=accuracy,
                precision_val=precision,
                recall_val=recall,
                f1=f1,
                samples=len(X),
                importance=importance,
                model_path=model_path
            )

            results[model_name] = {
                "accuracy": accuracy,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "samples": len(X),
                "top_features": list(importance.keys())[:5]
            }

        except Exception as e:
            logger.error(f"Error training {model_name}: {e}")
            results[model_name] = {"error": str(e)}

    return results
```

### 4.3 Загрузка данных для обучения

```python
def get_ml_training_data(bet_category: str):
    """
    Загружает данные обучения из БД для конкретной категории.

    Returns:
        X: list of feature vectors
        y: list of targets (0 или 1)
    """
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT features_json, target
        FROM ml_training_data
        WHERE bet_category = ? AND target IS NOT NULL
    """, (bet_category,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return None, None

    X = []
    y = []
    feature_names = list(ML_FEATURE_COLUMNS.keys())

    for features_json, target in rows:
        try:
            features = json.loads(features_json)
            # Преобразуем в вектор с дефолтами
            feature_values = [
                features.get(name, default)
                for name, default in ML_FEATURE_COLUMNS.items()
            ]
            X.append(feature_values)
            y.append(target)
        except:
            continue

    return X, y
```

### 4.4 Автоматическое переобучение

```python
def should_retrain_model(bet_category: str) -> bool:
    """
    Проверяет нужно ли переобучить модель.

    Переобучаем когда:
    1. Новых данных > 20% больше чем при обучении
    2. Недавняя точность значительно ниже точности модели
    """
    conn = get_db_connection()
    c = conn.cursor()

    # Получаем информацию о модели
    c.execute("""
        SELECT accuracy, samples_count, trained_at
        FROM ml_models
        WHERE model_type = ?
        ORDER BY trained_at DESC LIMIT 1
    """, (bet_category,))
    model = c.fetchone()

    if not model:
        conn.close()
        return False

    model_accuracy, model_samples, trained_at = model

    # Считаем текущие образцы
    c.execute("""
        SELECT COUNT(*) FROM ml_training_data
        WHERE bet_category = ? AND target IS NOT NULL
    """, (bet_category,))
    current_samples = c.fetchone()[0]

    conn.close()

    # Условие 1: много новых данных
    if current_samples > model_samples * 1.2:
        return True

    # Условие 2: недавняя точность упала (нужна дополнительная логика)

    return False
```

### 4.5 Периодическое обучение (Job)

```python
async def train_ensemble_models_job():
    """
    Периодическая задача для обучения моделей.
    Запускается каждые 24 часа.

    ВАЖНО: Категории должны совпадать с теми, что используются
    при сохранении данных!
    """
    categories = [
        "outcomes_home",
        "outcomes_away",
        "outcomes_draw",
        "totals_over",
        "totals_under",
        "btts"
    ]

    for category in categories:
        try:
            result = train_ensemble_models(category)
            if "error" not in result:
                log_learning_event(
                    "ensemble_trained",
                    f"Trained ensemble for {category}",
                    result
                )
        except Exception as e:
            logger.error(f"Error training {category}: {e}")
```

---

## 5. ПРЕДСКАЗАНИЯ

### 5.1 Загрузка моделей

```python
# Глобальный кэш моделей
_ensemble_models = {}

def load_ensemble_models(bet_category: str) -> dict:
    """
    Загружает все модели ансамбля для категории.
    Использует кэширование для производительности.

    Returns:
        dict: {model_name: {"model": sklearn_model, "feature_names": list}}
    """
    models = {}

    for model_name in ENSEMBLE_MODEL_TYPES.keys():
        cache_key = f"{model_name}_{bet_category}"

        # Проверяем кэш
        if cache_key in _ensemble_models:
            models[model_name] = _ensemble_models[cache_key]
            continue

        # Загружаем из файла
        model_path = f"{ML_MODELS_DIR}/{model_name}_{bet_category}.joblib"
        if os.path.exists(model_path):
            try:
                data = joblib.load(model_path)
                models[model_name] = data
                _ensemble_models[cache_key] = data  # Кэшируем
            except Exception as e:
                logger.error(f"Error loading {model_name}: {e}")

    return models
```

### 5.2 Предсказание ансамбля

```python
def get_ensemble_prediction(features: dict, bet_category: str) -> dict:
    """
    Получает предсказание от ансамбля моделей с голосованием.

    Args:
        features: словарь признаков
        bet_category: категория ставки

    Returns:
        {
            "prediction": int (предсказанный класс),
            "confidence": float (0-100),
            "votes": {model_name: {"pred": int, "prob": float, "class_name": str}},
            "agreement": float (0-1, согласие моделей),
            "consensus_boost": int (бонус за согласие),
            "available": bool
        }
    """
    result = {
        "prediction": None,
        "confidence": 50,
        "votes": {},
        "agreement": 0,
        "consensus_boost": 0,
        "available": False
    }

    if not ML_AVAILABLE:
        return result

    # Загружаем модели
    models = load_ensemble_models(bet_category)
    if not models:
        return result

    # Получаем предсказания от каждой модели
    predictions = []
    probabilities = []

    for model_name, model_data in models.items():
        try:
            model = model_data.get("model")
            feature_names = model_data.get("feature_names", [])

            if not model or not feature_names:
                continue

            # Подготовка вектора признаков
            feature_vector = np.array([[features.get(f, 0) for f in feature_names]])

            # Предсказание
            pred = model.predict(feature_vector)[0]
            prob = model.predict_proba(feature_vector)[0]
            pred_prob = prob[pred] if pred < len(prob) else 0.5

            # Маппинг классов
            if bet_category in ["outcomes_home", "outcomes_away", "outcomes_draw"]:
                class_names = {0: "away", 1: "draw", 2: "home"}
            else:
                class_names = {0: "no", 1: "yes"}
            class_name = class_names.get(pred, str(pred))

            # Вес модели
            weight = ENSEMBLE_MODEL_TYPES.get(model_name, {}).get("weight", 1.0)

            result["votes"][model_name] = {
                "pred": int(pred),
                "prob": round(pred_prob * 100, 1),
                "class_name": class_name,
                "weight": weight
            }

            predictions.append(pred)
            probabilities.append(pred_prob * weight)

        except Exception as e:
            logger.error(f"Error getting prediction from {model_name}: {e}")

    if not predictions:
        return result

    result["available"] = True

    # Подсчет голосов
    from collections import Counter
    vote_counts = Counter(predictions)
    most_common_pred, most_common_count = vote_counts.most_common(1)[0]

    result["prediction"] = int(most_common_pred)
    result["agreement"] = most_common_count / len(predictions)

    # Взвешенная средняя вероятность
    total_weight = sum(
        ENSEMBLE_MODEL_TYPES.get(m, {}).get("weight", 1.0)
        for m in result["votes"].keys()
    )
    weighted_prob = sum(probabilities) / total_weight if total_weight > 0 else 0.5

    # Базовая уверенность
    base_confidence = weighted_prob * 100

    # Бонус за консенсус
    if result["agreement"] >= 1.0:      # 100% согласие
        result["consensus_boost"] = 15
    elif result["agreement"] >= 0.67:   # 67%+ согласие
        result["consensus_boost"] = 8
    elif result["agreement"] >= 0.5:    # 50% согласие
        result["consensus_boost"] = 0
    else:                               # Разногласие
        result["consensus_boost"] = -10

    # Итоговая уверенность (ограничена 30-95%)
    result["confidence"] = min(95, max(30, base_confidence + result["consensus_boost"]))

    return result
```

### 5.3 Калиброванное предсказание

```python
def get_calibrated_prediction(features: dict, bet_category: str) -> dict:
    """
    Получает предсказание с применением калибровки.
    """
    # Базовое предсказание
    ensemble = get_ensemble_prediction(features, bet_category)

    if not ensemble["available"]:
        return ensemble

    # Применяем калибровку
    raw_confidence = ensemble["confidence"]
    calibrated_confidence = apply_calibration(bet_category, raw_confidence)

    # Применяем ROI-корректировку
    roi_adjustment, roi_reason = get_roi_adjustment(bet_category)

    final_confidence = min(95, max(30, calibrated_confidence + roi_adjustment))

    ensemble["confidence"] = final_confidence
    ensemble["calibration_applied"] = True
    ensemble["roi_adjustment"] = roi_adjustment

    return ensemble
```

---

## 6. СИСТЕМА КАЛИБРОВКИ

### 6.1 Принцип работы

```
Калибровка корректирует уверенность на основе исторической точности.

Пример:
- Система выдаёт 70% уверенности
- Но исторически такие предсказания выигрывают только 55%
- calibration_factor = 55/70 = 0.79
- Скорректированная уверенность = 70 × 0.79 = 55%

Диапазоны калибровки:
- "30-40", "40-50", "50-60", "60-70", "70-80", "80-90", "90-100"
```

### 6.2 Обновление калибровки

```python
def update_confidence_calibration(bet_category: str, confidence: float, is_correct: bool):
    """
    Обновляет калибровку после верификации результата.

    Args:
        bet_category: категория ставки
        confidence: уверенность предсказания (0-100)
        is_correct: правильным ли было предсказание
    """
    # Определяем диапазон
    band = get_confidence_band(confidence)  # "70-80" и т.д.

    conn = get_db_connection()
    c = conn.cursor()

    # Получаем текущие данные
    c.execute("""
        SELECT id, predicted_count, actual_wins
        FROM confidence_calibration
        WHERE bet_category = ? AND confidence_band = ?
    """, (bet_category, band))
    row = c.fetchone()

    if row:
        record_id, count, wins = row
        new_count = count + 1
        new_wins = wins + (1 if is_correct else 0)

        # Пересчитываем calibration_factor
        if new_count >= 10:  # Минимум 10 образцов для надёжности
            actual_rate = new_wins / new_count
            expected_rate = (int(band.split("-")[0]) + int(band.split("-")[1])) / 200
            factor = actual_rate / expected_rate if expected_rate > 0 else 1.0
            factor = max(0.65, min(1.35, factor))  # Ограничиваем ±35%
        else:
            factor = 1.0

        c.execute("""
            UPDATE confidence_calibration
            SET predicted_count = ?, actual_wins = ?, calibration_factor = ?,
                last_updated = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_count, new_wins, factor, record_id))
    else:
        c.execute("""
            INSERT INTO confidence_calibration
            (bet_category, confidence_band, predicted_count, actual_wins, calibration_factor)
            VALUES (?, ?, 1, ?, 1.0)
        """, (bet_category, band, 1 if is_correct else 0))

    conn.commit()
    conn.close()


def get_confidence_band(confidence: float) -> str:
    """Определяет диапазон уверенности."""
    if confidence < 40:
        return "30-40"
    elif confidence < 50:
        return "40-50"
    elif confidence < 60:
        return "50-60"
    elif confidence < 70:
        return "60-70"
    elif confidence < 80:
        return "70-80"
    elif confidence < 90:
        return "80-90"
    else:
        return "90-100"
```

### 6.3 Применение калибровки

```python
def apply_calibration(bet_category: str, raw_confidence: float) -> float:
    """
    Применяет калибровку к уверенности.

    Returns:
        Скорректированная уверенность (30-95%)
    """
    band = get_confidence_band(raw_confidence)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT calibration_factor, predicted_count
        FROM confidence_calibration
        WHERE bet_category = ? AND confidence_band = ?
    """, (bet_category, band))
    row = c.fetchone()
    conn.close()

    if row and row[1] >= 10:  # Минимум 10 образцов
        factor = row[0]
    else:
        factor = 1.0

    calibrated = raw_confidence * factor
    return max(30, min(95, calibrated))
```

---

## 7. ROI-АНАЛИТИКА И САМООБУЧЕНИЕ

### 7.1 Обновление ROI

```python
def update_roi_analytics(bet_category: str, condition_key: str,
                        is_win: bool, odds: float, stake: float, ev: float):
    """
    Обновляет ROI аналитику после верификации результата.

    Args:
        bet_category: категория ставки
        condition_key: "overall" или условие (например "high_injuries")
        is_win: выиграла ли ставка
        odds: коэффициент ставки
        stake: размер ставки
        ev: ожидаемая ценность
    """
    conn = get_db_connection()
    c = conn.cursor()

    # Вычисляем profit/loss
    if is_win:
        returned = stake * odds
        profit = returned - stake
    else:
        returned = 0
        profit = -stake

    # Получаем существующую запись
    c.execute("""
        SELECT id, total_bets, wins, losses, total_staked, total_returned, avg_odds, avg_ev
        FROM roi_analytics
        WHERE bet_category = ? AND condition_key = ?
    """, (bet_category, condition_key))
    row = c.fetchone()

    if row:
        record_id, total_bets, wins, losses, total_staked, total_returned, avg_odds, avg_ev = row

        # Обновляем
        new_total_bets = total_bets + 1
        new_wins = wins + (1 if is_win else 0)
        new_losses = losses + (0 if is_win else 1)
        new_total_staked = total_staked + stake
        new_total_returned = total_returned + returned
        new_avg_odds = (avg_odds * total_bets + odds) / new_total_bets
        new_avg_ev = (avg_ev * total_bets + ev) / new_total_bets

        # ROI = (returned - staked) / staked × 100%
        roi = ((new_total_returned - new_total_staked) / new_total_staked * 100) if new_total_staked > 0 else 0

        c.execute("""
            UPDATE roi_analytics SET
            total_bets = ?, wins = ?, losses = ?, total_staked = ?, total_returned = ?,
            roi_percent = ?, avg_odds = ?, avg_ev = ?, last_updated = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_total_bets, new_wins, new_losses, new_total_staked, new_total_returned,
              roi, new_avg_odds, new_avg_ev, record_id))
    else:
        # Создаём новую запись
        roi = ((returned - stake) / stake * 100) if stake > 0 else 0
        c.execute("""
            INSERT INTO roi_analytics
            (bet_category, condition_key, total_bets, wins, losses, total_staked, total_returned, roi_percent, avg_odds, avg_ev)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """, (bet_category, condition_key, 1 if is_win else 0, 0 if is_win else 1,
              stake, returned, roi, odds, ev))

    conn.commit()
    conn.close()
```

### 7.2 ROI-корректировка уверенности

```python
def get_roi_adjustment(bet_category: str) -> tuple:
    """
    Возвращает корректировку уверенности на основе ROI.

    Returns:
        (adjustment: int, reason: str or None)
    """
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        SELECT total_bets, roi_percent, avg_odds, wins, losses
        FROM roi_analytics
        WHERE bet_category = ? AND condition_key = 'overall'
    """, (bet_category,))
    row = c.fetchone()
    conn.close()

    if not row or row[0] < 15:  # Минимум 15 ставок
        return 0, None

    total_bets, roi, avg_odds, wins, losses = row

    # Логика корректировки по ROI
    if roi < -20:
        adjustment = -12
        reason = f"ROI: {roi:.1f}% ({total_bets} ставок) → -12%"
    elif roi < -10:
        adjustment = -8
        reason = f"ROI: {roi:.1f}% ({total_bets} ставок) → -8%"
    elif roi < 0:
        adjustment = -4
        reason = f"ROI: {roi:.1f}% ({total_bets} ставок) → -4%"
    elif roi < 10:
        adjustment = 3
        reason = f"ROI: +{roi:.1f}% ({total_bets} ставок) → +3%"
    elif roi < 25:
        adjustment = 6
        reason = f"ROI: +{roi:.1f}% ({total_bets} ставок) → +6%"
    else:
        adjustment = 10
        reason = f"ROI: +{roi:.1f}% ({total_bets} ставок) → +10%"

    return adjustment, reason
```

### 7.3 Извлечение условий для ROI

```python
def extract_feature_conditions(features: dict, bet_category: str) -> list:
    """
    Извлекает релевантные условия из признаков для ROI-трекинга.

    Returns:
        list условий: ["high_injuries", "away_favorite", ...]
    """
    conditions = []

    if not features:
        return conditions

    # Травмы
    home_injuries = features.get("home_injuries", 0)
    away_injuries = features.get("away_injuries", 0)
    if home_injuries > 8:
        conditions.append("home_injury_crisis")
    if away_injuries > 8:
        conditions.append("away_injury_crisis")
    if home_injuries + away_injuries > 12:
        conditions.append("high_total_injuries")

    # Позиции в таблице
    home_pos = features.get("home_position", 10)
    away_pos = features.get("away_position", 10)
    if away_pos < home_pos - 5:
        conditions.append("away_favorite")
    if home_pos < away_pos - 5:
        conditions.append("home_strong_favorite")

    # Форма
    home_win_rate = features.get("home_home_win_rate", 50)
    away_win_rate = features.get("away_away_win_rate", 30)
    if home_win_rate < 30:
        conditions.append("home_bad_form")
    if home_win_rate > 70:
        conditions.append("home_great_form")

    # Класс команды
    if features.get("elite_vs_underdog", 0) == 1:
        conditions.append("elite_vs_underdog")
    if features.get("class_mismatch", 0) > 2:
        conditions.append("class_mismatch")

    # Sharp money
    if features.get("sharp_money_detected", 0) == 1:
        conditions.append("sharp_money")

    # xG
    if features.get("both_underperforming", 0) == 1:
        conditions.append("xg_underperforming")

    # Дерби
    if features.get("is_derby", 0) == 1:
        conditions.append("derby")

    return conditions
```

### 7.4 Обучение на паттернах ошибок

```python
def update_error_pattern(bet_category: str, conditions: list, is_win: bool, confidence: float):
    """
    Обновляет паттерны ошибок для обучения системы.

    Записывает комбинации условий, при которых ставки проигрывают.
    """
    if not conditions:
        return

    # Создаём ключ из условий
    condition_key = "&".join(sorted(conditions))

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        SELECT id, total_predictions, wins, losses, avg_confidence_when_failed
        FROM feature_error_patterns
        WHERE bet_category = ? AND condition_key = ?
    """, (bet_category, condition_key))
    row = c.fetchone()

    if row:
        record_id, total, wins, losses, avg_conf = row
        new_total = total + 1
        new_wins = wins + (1 if is_win else 0)
        new_losses = losses + (0 if is_win else 1)

        # Обновляем среднюю уверенность при проигрыше
        if not is_win:
            new_avg_conf = (avg_conf * losses + confidence) / new_losses if new_losses > 0 else confidence
        else:
            new_avg_conf = avg_conf

        # Рассчитываем рекомендуемую корректировку
        if new_total >= 5:
            win_rate = new_wins / new_total
            if win_rate < 0.3:
                suggested = -15
            elif win_rate < 0.4:
                suggested = -10
            elif win_rate < 0.5:
                suggested = -5
            else:
                suggested = 0
        else:
            suggested = 0

        c.execute("""
            UPDATE feature_error_patterns SET
            total_predictions = ?, wins = ?, losses = ?,
            avg_confidence_when_failed = ?, suggested_adjustment = ?,
            last_updated = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_total, new_wins, new_losses, new_avg_conf, suggested, record_id))
    else:
        c.execute("""
            INSERT INTO feature_error_patterns
            (bet_category, condition_key, total_predictions, wins, losses, avg_confidence_when_failed, suggested_adjustment)
            VALUES (?, ?, 1, ?, ?, ?, 0)
        """, (bet_category, condition_key, 1 if is_win else 0, 0 if is_win else 1,
              confidence if not is_win else 0))

    conn.commit()
    conn.close()
```

---

## 8. ИНТЕГРАЦИЯ КОМПОНЕНТОВ

### 8.1 Полный цикл предсказания

```python
async def analyze_match_with_ml(match_data: dict, user_lang: str = "ru") -> dict:
    """
    Полный анализ матча с ML предсказаниями.

    Args:
        match_data: данные матча (команды, лига, дата...)
        user_lang: язык пользователя

    Returns:
        dict с предсказаниями и анализом
    """
    # 1. Собираем все данные
    home_team = match_data["home_team"]
    away_team = match_data["away_team"]
    league = match_data["league"]

    # Параллельно загружаем данные
    form_data = await get_form_data(home_team, away_team)
    standings = await get_standings(league)
    odds = await get_odds(home_team, away_team)
    h2h = await get_h2h(home_team, away_team)
    xg_data = await get_xg_data(home_team, away_team)
    injuries = await get_injuries(home_team, away_team)
    # ... другие данные

    # 2. Извлекаем признаки
    features = extract_features(
        home_form=form_data["home"],
        away_form=form_data["away"],
        standings=standings,
        odds=odds,
        h2h=h2h,
        home_team=home_team,
        away_team=away_team,
        xg_data=xg_data,
        injuries=injuries,
        # ... другие параметры
    )

    # 3. Получаем ML предсказания для всех категорий
    predictions = {}
    categories = ["outcomes_home", "outcomes_away", "outcomes_draw",
                  "totals_over", "totals_under", "btts"]

    for category in categories:
        ensemble = get_ensemble_prediction(features, category)
        if ensemble["available"]:
            # Применяем калибровку
            ensemble["confidence"] = apply_calibration(category, ensemble["confidence"])

            # Применяем ROI-корректировку
            roi_adj, roi_reason = get_roi_adjustment(category)
            ensemble["confidence"] = max(30, min(95, ensemble["confidence"] + roi_adj))
            ensemble["roi_adjustment"] = roi_adj

            predictions[category] = ensemble

    # 4. Выбираем лучшую ставку
    best_bet = select_best_bet(predictions, odds)

    # 5. Рассчитываем EV и размер ставки
    if best_bet:
        best_bet["ev"] = calculate_ev(best_bet["confidence"], best_bet["odds"])
        best_bet["stake"] = calculate_kelly_stake(best_bet["confidence"], best_bet["odds"])

    return {
        "match": match_data,
        "features": features,
        "predictions": predictions,
        "best_bet": best_bet
    }
```

### 8.2 Сохранение предсказания

```python
def save_prediction_with_ml(user_id: int, match_data: dict, bet_type: str,
                           category: str, confidence: float, odds: float,
                           features: dict, bet_rank: int = 1) -> int:
    """
    Сохраняет предсказание и данные для ML обучения.

    Returns:
        prediction_id
    """
    conn = get_db_connection()
    c = conn.cursor()

    # Вычисляем EV и stake
    ev = calculate_ev(confidence, odds)
    stake = calculate_kelly_stake(confidence, odds)

    # Сохраняем предсказание
    c.execute("""
        INSERT INTO predictions
        (user_id, match_id, home_team, away_team, bet_type, bet_category,
         confidence, odds, bet_rank, league_code, ml_features_json,
         expected_value, stake_percent, match_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, match_data["id"], match_data["home"], match_data["away"],
          bet_type, category, confidence, odds, bet_rank, match_data["league"],
          json.dumps(features), ev, stake, match_data["kickoff"]))

    prediction_id = c.lastrowid
    conn.commit()
    conn.close()

    # Сохраняем данные для ML обучения
    save_ml_training_data(prediction_id, category, features, target=None, bet_rank=bet_rank)

    return prediction_id


def save_ml_training_data(prediction_id: int, bet_category: str,
                         features: dict, target: int = None, bet_rank: int = 1):
    """
    Сохраняет признаки для ML обучения.
    Target будет заполнен после верификации результата.
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            INSERT OR REPLACE INTO ml_training_data
            (prediction_id, bet_category, features_json, target, bet_rank)
            VALUES (?, ?, ?, ?, ?)
        """, (prediction_id, bet_category, json.dumps(features), target, bet_rank))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving ML training data: {e}")
```

### 8.3 Верификация результата

```python
async def verify_prediction(prediction_id: int, actual_result: str):
    """
    Верифицирует результат предсказания и обновляет все ML компоненты.

    Args:
        prediction_id: ID предсказания
        actual_result: фактический результат матча ("home", "draw", "away", score...)
    """
    conn = get_db_connection()
    c = conn.cursor()

    # Получаем данные предсказания
    c.execute("""
        SELECT bet_type, bet_category, confidence, odds, ml_features_json,
               expected_value, stake_percent
        FROM predictions WHERE id = ?
    """, (prediction_id,))
    pred = c.fetchone()

    if not pred:
        conn.close()
        return

    bet_type, category, confidence, odds, features_json, ev, stake = pred
    features = json.loads(features_json) if features_json else {}

    # Определяем правильность
    is_correct = check_prediction_correctness(bet_type, actual_result)

    # 1. Обновляем предсказание
    c.execute("""
        UPDATE predictions
        SET result = ?, is_correct = ?, checked_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (actual_result, is_correct, prediction_id))

    # 2. Обновляем ML training target
    target = 1 if is_correct else 0
    c.execute("""
        UPDATE ml_training_data SET target = ? WHERE prediction_id = ?
    """, (target, prediction_id))

    conn.commit()
    conn.close()

    # 3. Обновляем калибровку
    update_confidence_calibration(category, confidence, is_correct)

    # 4. Обновляем ROI аналитику
    conditions = extract_feature_conditions(features, category)
    for condition in conditions:
        update_roi_analytics(category, condition, is_correct, odds, stake, ev)
    update_roi_analytics(category, "overall", is_correct, odds, stake, ev)

    # 5. Обновляем паттерны ошибок
    if not is_correct:
        update_error_pattern(category, conditions, is_correct, confidence)

    # 6. Обновляем обучение по лигам
    league = get_prediction_league(prediction_id)
    update_league_learning(league, category, is_correct)

    # 7. Проверяем нужно ли переобучить модель
    if should_retrain_model(category):
        result = train_ensemble_models(category)
        if "error" not in result:
            log_learning_event("model_retrained", f"Retrained {category}", result)
```

---

## 9. КОНФИГУРАЦИЯ

### 9.1 Основные настройки (config.py)

```python
# ===== ML CONFIGURATION =====

# Директория для сохранения моделей
ML_MODELS_DIR = "ml_models"

# Минимум образцов для обучения
ML_MIN_SAMPLES = 50

# Проверка доступности ML библиотек
try:
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

# Интервал автоматического обучения (в секундах)
ML_TRAINING_INTERVAL = 86400  # 24 часа

# Максимальная/минимальная уверенность
ML_MIN_CONFIDENCE = 30
ML_MAX_CONFIDENCE = 95

# Минимальная уверенность для ставки
MIN_CONFIDENCE_FOR_BET = 55

# Минимальный EV для ставки
MIN_EV_FOR_BET = 5.0  # 5%
```

### 9.2 Настройки расчётов

```python
# ===== BETTING CALCULATIONS =====

# Kelly Criterion - фракция (25% от полного Kelly)
KELLY_FRACTION = 0.25

# Максимальный размер ставки
MAX_STAKE_PERCENT = 10.0

# Минимальный размер ставки
MIN_STAKE_PERCENT = 1.0


def calculate_ev(confidence: float, odds: float) -> float:
    """
    Рассчитывает Expected Value (ожидаемую ценность).

    EV = (probability × odds) - 1
    В процентах: ((confidence/100) × odds - 1) × 100
    """
    probability = confidence / 100
    ev = (probability * odds - 1) * 100
    return round(ev, 1)


def calculate_kelly_stake(confidence: float, odds: float) -> float:
    """
    Рассчитывает размер ставки по Kelly Criterion.

    Kelly % = (odds × probability - 1) / odds
    Fractional Kelly = Kelly % × KELLY_FRACTION
    """
    probability = confidence / 100

    if odds <= 1:
        return MIN_STAKE_PERCENT

    kelly = ((odds * probability - 1) / odds) * 100
    fractional_kelly = kelly * KELLY_FRACTION

    # Ограничиваем диапазон
    stake = max(MIN_STAKE_PERCENT, min(MAX_STAKE_PERCENT, fractional_kelly))

    return round(stake, 2)
```

---

## 10. ИЗВЕСТНЫЕ ПРОБЛЕМЫ И TODO

### 10.1 Критические проблемы (требуют исправления)

#### Проблема 1: Несовпадение категорий обучения

**Описание:** В автоматическом job обучения используются категории `["match_result", "totals", "btts"]`, но данные сохраняются с категориями `["outcomes_home", "outcomes_away", "outcomes_draw", "totals_over", "totals_under", "btts"]`.

**Исправление:**
```python
# Файл: bot_secure.py, строка ~18640
# Было:
categories = ["match_result", "totals", "btts"]

# Должно быть:
categories = ["outcomes_home", "outcomes_away", "outcomes_draw",
              "totals_over", "totals_under", "btts"]
```

#### Проблема 2: Target бинарный вместо классового

**Описание:** Target показывает "правильность предсказания" (1/0), а не класс результата. Это концептуальная проблема - модель учится предсказывать "будет ли бот прав", а не "какой будет результат".

**Возможные решения:**
1. Оставить как есть - модель предсказывает качество своих предсказаний (мета-модель)
2. Изменить target на класс результата:
   - Для outcomes: 0=away, 1=draw, 2=home
   - Для totals: 0=under, 1=over
   - Для btts: 0=no, 1=yes

### 10.2 Рекомендуемые улучшения

1. **Валидация признаков** - добавить проверку на NaN/Inf перед сохранением
2. **Отдельные наборы признаков** - разные признаки для разных категорий
3. **Увеличить минимум для калибровки** - с 10 до 20-30 образцов
4. **Feature selection** - автоматический отбор важных признаков
5. **Cross-validation** - использовать при обучении
6. **Hyperparameter tuning** - автоматический подбор параметров

### 10.3 Мониторинг

```python
def get_ml_system_stats() -> dict:
    """Возвращает статистику ML системы для мониторинга."""
    conn = get_db_connection()
    c = conn.cursor()

    stats = {}

    # Количество образцов по категориям
    c.execute("""
        SELECT bet_category,
               COUNT(*) as total,
               SUM(CASE WHEN target IS NOT NULL THEN 1 ELSE 0 END) as verified,
               SUM(CASE WHEN target = 1 THEN 1 ELSE 0 END) as correct
        FROM ml_training_data
        GROUP BY bet_category
    """)
    stats["training_data"] = {row[0]: {"total": row[1], "verified": row[2], "correct": row[3]}
                              for row in c.fetchall()}

    # Модели
    c.execute("""
        SELECT bet_category, model_name, accuracy, samples_count, trained_at
        FROM ensemble_models
        ORDER BY trained_at DESC
    """)
    stats["models"] = [{"category": row[0], "name": row[1], "accuracy": row[2],
                       "samples": row[3], "trained": row[4]} for row in c.fetchall()]

    # ROI по категориям
    c.execute("""
        SELECT bet_category, total_bets, roi_percent
        FROM roi_analytics
        WHERE condition_key = 'overall'
    """)
    stats["roi"] = {row[0]: {"bets": row[1], "roi": row[2]} for row in c.fetchall()}

    conn.close()
    return stats
```

---

## ПРИЛОЖЕНИЕ A: SQL для создания всех таблиц

```sql
-- Выполнить для создания всех ML таблиц

CREATE TABLE IF NOT EXISTS ml_training_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER,
    bet_category TEXT,
    features_json TEXT,
    target INTEGER,
    bet_rank INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);
CREATE INDEX IF NOT EXISTS idx_ml_training_category ON ml_training_data(bet_category);
CREATE INDEX IF NOT EXISTS idx_ml_training_target ON ml_training_data(target);

CREATE TABLE IF NOT EXISTS ml_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type TEXT,
    accuracy REAL,
    precision_score REAL,
    recall_score REAL,
    f1_score REAL,
    samples_count INTEGER,
    model_path TEXT,
    trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ensemble_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT,
    model_type TEXT,
    bet_category TEXT,
    accuracy REAL,
    precision_val REAL,
    recall_val REAL,
    f1_score REAL,
    samples_count INTEGER,
    feature_importance TEXT,
    model_path TEXT,
    trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_name, bet_category)
);

CREATE TABLE IF NOT EXISTS confidence_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_category TEXT,
    confidence_band TEXT,
    predicted_count INTEGER DEFAULT 0,
    actual_wins INTEGER DEFAULT 0,
    calibration_factor REAL DEFAULT 1.0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bet_category, confidence_band)
);

CREATE TABLE IF NOT EXISTS roi_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_category TEXT,
    condition_key TEXT,
    total_bets INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_staked REAL DEFAULT 0,
    total_returned REAL DEFAULT 0,
    roi_percent REAL DEFAULT 0,
    avg_odds REAL DEFAULT 0,
    avg_ev REAL DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bet_category, condition_key)
);

CREATE TABLE IF NOT EXISTS learning_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT,
    pattern_key TEXT,
    bet_category TEXT,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_predictions INTEGER DEFAULT 0,
    avg_confidence REAL DEFAULT 0,
    description TEXT,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pattern_type, pattern_key, bet_category)
);

CREATE TABLE IF NOT EXISTS feature_error_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_category TEXT,
    condition_key TEXT,
    total_predictions INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    avg_confidence_when_failed REAL,
    suggested_adjustment INTEGER,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bet_category, condition_key)
);

CREATE TABLE IF NOT EXISTS league_learning (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_code TEXT,
    bet_category TEXT,
    total_predictions INTEGER DEFAULT 0,
    correct_predictions INTEGER DEFAULT 0,
    accuracy REAL DEFAULT 0,
    avg_confidence REAL DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(league_code, bet_category)
);

CREATE TABLE IF NOT EXISTS learning_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    description TEXT,
    data_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## ПРИЛОЖЕНИЕ B: Минимальный пример интеграции

```python
"""
Минимальный пример использования ML системы.
"""
import json
import sqlite3
import numpy as np
import joblib
import os

from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

# Конфигурация
ML_MODELS_DIR = "ml_models"
ML_MIN_SAMPLES = 50
DB_PATH = "betting.db"

# Признаки (упрощённый набор)
FEATURES = {
    "home_wins": 0,
    "away_wins": 0,
    "home_position": 10,
    "away_position": 10,
    "odds_home": 2.5,
    "odds_away": 3.0,
}

def get_db():
    return sqlite3.connect(DB_PATH)

def train_model(category: str):
    """Обучает модель для категории."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT features_json, target FROM ml_training_data WHERE bet_category = ? AND target IS NOT NULL", (category,))
    rows = c.fetchall()
    conn.close()

    if len(rows) < ML_MIN_SAMPLES:
        return None

    X = []
    y = []
    for features_json, target in rows:
        features = json.loads(features_json)
        X.append([features.get(k, v) for k, v in FEATURES.items()])
        y.append(target)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)

    model = GradientBoostingClassifier(n_estimators=100, max_depth=5)
    model.fit(X_train, y_train)

    accuracy = accuracy_score(y_test, model.predict(X_test))

    os.makedirs(ML_MODELS_DIR, exist_ok=True)
    joblib.dump({"model": model, "features": list(FEATURES.keys())}, f"{ML_MODELS_DIR}/{category}.joblib")

    return {"accuracy": accuracy, "samples": len(rows)}

def predict(features: dict, category: str) -> dict:
    """Делает предсказание."""
    path = f"{ML_MODELS_DIR}/{category}.joblib"
    if not os.path.exists(path):
        return {"available": False}

    data = joblib.load(path)
    model = data["model"]
    feature_names = data["features"]

    X = np.array([[features.get(f, FEATURES.get(f, 0)) for f in feature_names]])

    pred = model.predict(X)[0]
    prob = model.predict_proba(X)[0]

    return {
        "available": True,
        "prediction": int(pred),
        "confidence": float(max(prob) * 100)
    }

# Использование
if __name__ == "__main__":
    # Обучение
    result = train_model("outcomes_home")
    print(f"Training result: {result}")

    # Предсказание
    features = {
        "home_wins": 3,
        "away_wins": 1,
        "home_position": 5,
        "away_position": 12,
        "odds_home": 1.8,
        "odds_away": 4.5,
    }
    prediction = predict(features, "outcomes_home")
    print(f"Prediction: {prediction}")
```

---

**Документ создан:** 2026-01-13
**Версия:** 1.0
**Источник:** betting-bot ML system audit
