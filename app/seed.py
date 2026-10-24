"""Начальный список растений. Загружается один раз, когда таблица plants пустая.

Интервалы полива — ориентир для подоконника; под фитолампой в тёплой комнате
зимой поливать придётся чаще. Всё можно поправить в карточке растения.
"""
import logging
from pathlib import Path

from app import photos
from app.care import now_str
from app.config import SEED_PHOTOS_DIR
from app.db import PLANT_FIELDS, add_photo, get_db, insert_plant
from app.diseases import diseases_for
from app.facts import facts_for
from app.repotting import repotting_for

log = logging.getLogger(__name__)

CACTUS = dict(
    water_summer_days=10, water_winter_days=30,
    water_how="Обильно до стока из дренажа, затем полная просушка. При прохладной зимовке почти не поливать.",
    soil="полностью сухая",
    temperature="20–30 °C; зимой 8–12 °C (прохладная зимовка)",
    light="прямое солнце",
    air_humidity="низкая (30–40%)",
    feed_days=30, repot_years=2,
)

BENJAMINA = dict(
    water_summer_days=5, water_winter_days=8,
    water_how="Тёплой отстоянной водой, воду из поддона слить через 15 минут.",
    soil="верхние 2–3 см сухие",
    temperature="18–25 °C; зимой не ниже 16 °C, без сквозняков",
    light="яркий рассеянный, утреннее/вечернее солнце",
    air_humidity="50–70%",
    feed_days=14, repot_years=2, spray_days=3,
    toxic="сок токсичен для животных",
)
BENJAMINA_NOTE = "Сбрасывает листья при перестановке, сквозняке, на холодном подоконнике и при переливе — место не менять."
VARIEGATED_LIGHT = "яркий рассеянный — пёстрым сортам нужно больше света, иначе зеленеют"

CHLOROPHYTUM = dict(
    species="Chlorophytum comosum",
    water_summer_days=4, water_winter_days=8,
    water_how="Обильно, вода в поддоне не должна стоять.",
    soil="верх подсох, внутри слегка влажная",
    temperature="18–24 °C; зимой не ниже 12 °C",
    light="яркий рассеянный, переносит полутень",
    air_humidity="40–60%",
    feed_days=14, repot_years=1,
)
CHLOROPHYTUM_NOTE = "Деток можно укоренять в воде или сразу в грунте. Бурые кончики — жёсткая вода или пересушка."

PLANTS = [
    {
        **CACTUS, "photo": "1.jpg",
        "name": "Опунция цилиндрика кристата",
        "water_ml": 150,
        "species": "Austrocylindropuntia cylindrica f. cristata",
        "notes": "Гребенчатая форма. Длинный цилиндрический побег — реверсия к обычной форме: "
                 "его лучше срезать, иначе он заберёт силы у гребня. Есть глохидии — работать в перчатках.",
    },
    {
        **CACTUS, "photo": "2.jpg",
        "name": "Опунция мелковолосистая",
        "water_ml": 150,
        "species": "Opuntia microdasys",
        "notes": "Глохидии («пушистые точки») легко впиваются в кожу — только в перчатках, "
                 "при пересадке обернуть газетой. Не опрыскивать.",
    },
    {
        **CACTUS, "photo": "14.jpg",
        "name": "Стетсония булавовидная",
        "water_ml": 200,
        "species": "Stetsonia coryne",
        "temperature": "20–30 °C; зимой 10–15 °C, не ниже 8 °C",
        "notes": "«Кактус-зубочистка»: длинные острые колючки. Зимой — сухое содержание, холод + сырость = гниль. "
                 "В горшке несколько стеблей, при пересадке можно рассадить.",
    },
    {
        "photo": "3.jpg",
        "name": "Авония бумагоподобная",
        "water_ml": 30,
        "species": "Avonia papyracea",
        "water_summer_days": 14, "water_winter_days": 30,
        "water_how": "Небольшим объёмом по краю горшка; в летнюю жару и зимой — совсем понемногу.",
        "soil": "полностью сухая несколько дней",
        "temperature": "18–28 °C; зимой 10–15 °C",
        "light": "максимум света: прямое солнце или фитолампа 12–14 ч",
        "air_humidity": "низкая",
        "feed_days": 60, "repot_years": 3,
        "notes": "Очень чувствительна к переливу — только минеральный грунт (пемза, цеолит). "
                 "Белые чешуйки защищают от солнца, не трогать. Растёт очень медленно.",
    },
    {
        "photo": "4.jpg",
        "name": "Эхеверия",
        "water_ml": 50,
        "species": "Echeveria sp. (похожа на 'Lola')",
        "water_summer_days": 10, "water_winter_days": 25,
        "water_how": "По краю горшка, вода не должна попадать в розетку.",
        "soil": "полностью сухая",
        "temperature": "20–27 °C; зимой 10–15 °C",
        "light": "прямое солнце или фитолампа 12–14 ч; без света вытягивается",
        "air_humidity": "низкая",
        "feed_days": 30, "repot_years": 2,
        "notes": "Восковой налёт на листьях не стирать, не опрыскивать. Засохшие нижние листья удалять.",
    },
    {
        "photo": "16.jpg",
        "name": "Хавортия",
        "water_ml": 50,
        "species": "Haworthia sp. (группа retusa / comptoniana)",
        "water_summer_days": 14, "water_winter_days": 30,
        "water_how": "По краю горшка, в розетку не лить.",
        "soil": "полностью сухая",
        "temperature": "20–27 °C; зимой 10–15 °C",
        "light": "яркий рассеянный, без полуденного солнца — «окошки» листьев обгорают",
        "air_humidity": "низкая",
        "feed_days": 42, "repot_years": 3,
        "notes": "Минеральный субстрат — правильно. Горшок широкий и неглубокий. "
                 "Покраснение листьев — стресс от избытка света.",
    },
    {
        "photo": "21.jpg",
        "name": "Замиокулькас",
        "water_ml": 300,
        "species": "Zamioculcas zamiifolia",
        "water_summer_days": 14, "water_winter_days": 30,
        "water_how": "Обильно, но только после полной просушки.",
        "soil": "полностью сухая",
        "temperature": "18–26 °C; зимой не ниже 15 °C",
        "light": "яркий рассеянный, терпит полутень",
        "air_humidity": "любая",
        "feed_days": 30, "repot_years": 3, "wipe_days": 30,
        "toxic": "ядовит, сок раздражает кожу и слизистые",
        "notes": "Клубни запасают воду — главный риск перелив. Пересаживать, когда клубни распирают горшок.",
    },
    {
        "photo": "5.jpg",
        "name": "Бокарнея (нолина)",
        "water_ml": 250,
        "species": "Beaucarnea recurvata",
        "water_summer_days": 18, "water_winter_days": 40,
        "water_how": "Обильно, лучше нижним поливом через поддон на 20–30 минут.",
        "soil": "полностью сухая",
        "temperature": "20–25 °C; зимой 10–15 °C",
        "light": "яркий, прямое солнце допустимо",
        "air_humidity": "низкая, сухой воздух переносит",
        "feed_days": 30, "repot_years": 4,
        "notes": "Ствол-каудекс запасает воду — главный риск перелив. Сухие кончики листьев — от сухого воздуха, "
                 "можно подстригать. Горшок широкий и неглубокий.",
    },
    {
        "photo": "15.jpg",
        "name": "Драцена",
        "water_ml": 300,
        "species": "Dracaena marginata",
        "water_summer_days": 8, "water_winter_days": 17,
        "water_how": "Отстоянной водой комнатной температуры.",
        "soil": "верхние 2–3 см сухие",
        "temperature": "18–25 °C; зимой не ниже 15 °C",
        "light": "яркий рассеянный",
        "air_humidity": "40–60%",
        "feed_days": 28, "repot_years": 3, "wipe_days": 14,
        "toxic": "токсична для кошек и собак",
        "notes": "Чувствительна к фтору и солям в воде — от них бурые кончики. "
                 "Сбрасывать нижние листья с возрастом — нормально.",
    },
    {
        "photo": "8.jpg",
        "name": "Фикус каучуконосный",
        "water_ml": 300,
        "species": "Ficus elastica",
        "water_summer_days": 6, "water_winter_days": 12,
        "water_how": "Обильно, лишнюю воду из поддона слить.",
        "soil": "верхние 3–4 см сухие",
        "temperature": "18–25 °C; зимой не ниже 15 °C",
        "light": "яркий рассеянный",
        "air_humidity": "40–60%",
        "feed_days": 14, "repot_years": 1, "wipe_days": 14,
        "toxic": "млечный сок раздражает кожу, токсичен для животных",
        "notes": "Пока молодой — пересадка ежегодно, взрослый — раз в 2–3 года. "
                 "Для ветвления весной можно прищипнуть верхушку.",
    },
    {
        "photo": "23.jpg",
        "name": "Фикус каучуконосный (большой, зелёный горшок)",
        "water_ml": 700,
        "species": "Ficus elastica",
        "water_summer_days": 7, "water_winter_days": 14,
        "water_how": "Обильно, лишнюю воду из поддона слить.",
        "soil": "верхние 3–4 см сухие",
        "temperature": "18–25 °C; зимой не ниже 15 °C",
        "light": "яркий рассеянный — стоит далеко от окна, нужна досветка",
        "air_humidity": "40–60%",
        "feed_days": 14, "repot_years": 2, "wipe_days": 14,
        "toxic": "млечный сок раздражает кожу, токсичен для животных",
        "notes": "Взрослый, ветвистый. Горшок для такой кроны маловат — весной пересадить на 2–3 см шире.",
    },
    {
        **BENJAMINA, "photo": "6.jpg",
        "name": "Фикус Бенджамина «Эстер»",
        "water_ml": 400,
        "species": "Ficus benjamina 'Esther'",
        "notes": f"{BENJAMINA_NOTE} Сейчас крона редкая — похоже на листопад: проверить сквозняк от окна и полив.",
    },
    {
        **BENJAMINA, "photo": "13.jpg",
        "name": "Фикус Бенджамина «Кинки»",
        "water_ml": 250,
        "species": "Ficus benjamina 'Kinky'",
        "light": VARIEGATED_LIGHT,
        "notes": f"{BENJAMINA_NOTE} Компактный сорт с волнистыми пёстрыми листьями.",
    },
    {
        **BENJAMINA, "photo": "17.jpg",
        "name": "Фикус Бенджамина пёстрый",
        "water_ml": 300,
        "species": "Ficus benjamina (сорт не определён)",
        "light": VARIEGATED_LIGHT, "repot_years": 3,
        "notes": f"{BENJAMINA_NOTE} Опавшие листья с поверхности грунта убирать — в них заводятся вредители.",
    },
    {
        "photo": "19.jpg",
        "name": "Фикус «Гинсенг»",
        "water_ml": 200,
        "species": "Ficus microcarpa 'Ginseng'",
        "water_summer_days": 5, "water_winter_days": 8,
        "water_how": "Умеренно: не заливать и не пересушивать.",
        "soil": "верхние 1–2 см сухие",
        "temperature": "18–25 °C; зимой не ниже 15 °C",
        "light": "яркий рассеянный",
        "air_humidity": "50–70%",
        "feed_days": 14, "repot_years": 2, "spray_days": 3,
        "toxic": "сок токсичен для животных",
        "notes": "Утолщённый корень загнивает при переливе. Формирующая обрезка весной. "
                 "Сбрасывает листья при перестановке.",
    },
    {
        "photo": "22.jpg",
        "name": "Монстера",
        "water_ml": 600,
        "species": "Monstera deliciosa",
        "water_summer_days": 6, "water_winter_days": 12,
        "water_how": "Обильно тёплой водой, воду из поддона слить.",
        "soil": "верхние 3–5 см сухие",
        "temperature": "20–27 °C; зимой не ниже 16 °C",
        "light": "яркий рассеянный, без прямого полуденного солнца",
        "air_humidity": "50–70%",
        "feed_days": 14, "repot_years": 2, "wipe_days": 14,
        "toxic": "токсична для кошек и собак (оксалаты кальция)",
        "notes": "Нужна опора. Воздушные корни не срезать — направлять в грунт или к опоре. "
                 "Молодую пересаживать ежегодно.",
    },
    {
        **CHLOROPHYTUM, "photo": "9.jpg",
        "name": "Хлорофитум (большой, на стеллаже)",
        "water_ml": 400,
        "species": "Chlorophytum comosum 'Variegatum'",
        "notes": f"{CHLOROPHYTUM_NOTE} Есть пожелтевшие листья — вероятно, горшок тесный, весной пересадить.",
    },
    {
        **CHLOROPHYTUM, "photo": "10.jpg",
        "name": "Хлорофитум (жёлтый горшок)",
        "water_ml": 200,
        "notes": f"{CHLOROPHYTUM_NOTE} Вид определён по фото под фитолампой — проверить.",
    },
    {
        **CHLOROPHYTUM, "photo": "11.jpg",
        "name": "Хлорофитум (серый горшок, №1)",
        "water_ml": 200,
        "notes": CHLOROPHYTUM_NOTE,
    },
    {
        **CHLOROPHYTUM, "photo": "12.jpg",
        "name": "Хлорофитум (серый горшок, №2)",
        "water_ml": 200,
        "notes": CHLOROPHYTUM_NOTE,
    },
    {
        "photo": "7.jpg",
        "name": "Гербера",
        "water_ml": 150,
        "species": "Gerbera jamesonii",
        "water_summer_days": 3, "water_winter_days": 6,
        "water_how": "По краю горшка или через поддон, на розетку не лить.",
        "soil": "верхний 1 см сухой",
        "temperature": "20–24 °C; зимой 14–16 °C",
        "light": "яркий, 12–14 ч (зимой фитолампа)",
        "air_humidity": "50–60%, листья не опрыскивать",
        "feed_days": 14, "repot_years": 1,
        "notes": "Корневую шейку не заглублять и не заливать — гниёт. "
                 "На фото сильно увядшая: если земля мокрая — перелив, проверить корни.",
    },
    {
        "photo": "18.jpg",
        "name": "Орхидея фаленопсис",
        "water_ml": 100,
        "species": "Phalaenopsis",
        "water_summer_days": 8, "water_winter_days": 12,
        "water_how": "Погружением на 15–20 минут, затем дать стечь. Вода не должна оставаться в пазухах листьев.",
        "soil": "кора сухая, корни серебристые",
        "temperature": "20–25 °C днём, ночью на 4–6 °C ниже (стимул цветения)",
        "light": "яркий рассеянный, 12 ч",
        "air_humidity": "50–70%",
        "feed_days": 21, "repot_years": 2,
        "notes": "Цветоносы с бутонами — не переставлять и не поворачивать, иначе бутоны опадут. "
                 "Пересаживать после цветения в кору.",
    },
    {
        "photo": "20.jpg",
        "name": "Цикас поникающий",
        "water_ml": 400,
        "species": "Cycas revoluta",
        "water_summer_days": 8, "water_winter_days": 25,
        "water_how": "По краю горшка, не лить на «шишку» и в центр розетки.",
        "soil": "верхние 3–5 см сухие",
        "temperature": "22–26 °C; зимой 12–16 °C",
        "light": "яркий, прямое солнце допустимо (приучать постепенно)",
        "air_humidity": "40–60%",
        "feed_days": 35, "repot_years": 4,
        "toxic": "⚠️ все части сильно ядовиты (животные, дети)",
        "notes": "Новый ярус листьев — раз в год. Пока растут новые листья, не подкармливать и не переставлять — "
                 "листья вырастут кривыми.",
    },
]

# справочные тексты по виду: колонка -> функция, возвращающая текст по латинскому названию
REFERENCE = {"facts": facts_for, "diseases": diseases_for, "repotting": repotting_for}


def seed_if_empty() -> None:
    with get_db() as db:
        if db.execute("SELECT 1 FROM plants LIMIT 1").fetchone():
            return
        created_at = now_str()
        for plant in PLANTS:
            values = {field: plant.get(field) for field in PLANT_FIELDS}
            for field in ("species", "location", "water_how", "soil", "temperature",
                          "light", "air_humidity", "toxic", "notes"):
                values[field] = values[field] or ""
            for field, text_for in REFERENCE.items():
                values[field] = text_for(values["species"])
            plant_id = insert_plant(db, values, created_at)

            photo_path = Path(SEED_PHOTOS_DIR) / plant["photo"] if plant.get("photo") else None
            if photo_path and photo_path.exists():
                data, thumb = photos.process(photo_path.read_bytes(), photo_path.name)
                add_photo(db, plant_id, data, thumb, created_at)
        log.info("Добавлено растений: %d", len(PLANTS))


def fill_missing() -> None:
    """Дописывает справочные тексты и объём полива растениям, у которых их ещё нет (например, в старой БД)."""
    water_ml = {plant["name"]: plant["water_ml"] for plant in PLANTS}
    with get_db() as db:
        for plant in db.execute("SELECT * FROM plants").fetchall():
            for field, text_for in REFERENCE.items():
                text = text_for(plant["species"])
                if not plant[field] and text:
                    # имя колонки — из константы REFERENCE, не из ввода пользователя
                    db.execute(f"UPDATE plants SET {field} = ? WHERE id = ?", (text, plant["id"]))
            if plant["water_ml"] is None and plant["name"] in water_ml:
                db.execute("UPDATE plants SET water_ml = ? WHERE id = ?", (water_ml[plant["name"]], plant["id"]))
