import json
import os
import sys
from datetime import datetime

import gspread
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials

DEFAULT_GAMES_DATA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "games_data.json"
)

# Загружаем переменные из .env
load_dotenv()

# Настройка кодировки для Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')


class SheetsReader:
    """Чтение данных из Google Sheets (формат: ключ-значение)"""

    # Служебные вкладки
    SERVICE_SHEETS = ["📰 Новости по играм", "📦 Add-ons", "📚 KNOWLEDGE_BASE"]

    def __init__(self):
        self.credentials_file = os.getenv(
            "GOOGLE_SHEETS_CREDENTIALS", "credentials.json")
        self.sheet_id = os.getenv("GOOGLE_SHEETS_ID")
        self.client = None
        self.spreadsheet = None
        self._authenticate()

    def _authenticate(self):
        """Аутентификация в Google Sheets"""
        try:
            if not os.path.exists(self.credentials_file):
                print(f"❌ Файл {self.credentials_file} не найден!")
                return False

            scope = ['https://spreadsheets.google.com/feeds',
                     'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_name(
                self.credentials_file, scope)
            self.client = gspread.authorize(creds)
            self.spreadsheet = self.client.open_by_key(self.sheet_id)
            print(f"✅ Подключено к таблице: {self.spreadsheet.title}")
            return True
        except Exception as e:
            print(f"❌ Ошибка подключения: {e}")
            return False

    def get_all_sheets(self):
        """Получение списка всех вкладок"""
        if not self.spreadsheet:
            return []
        return self.spreadsheet.worksheets()

    def read_game_data(self, worksheet):
        """
        Чтение данных игры из вкладки (формат: ключ-значение)
        Первая колонка - поле, вторая - пояснение (пропускаем), третья - значение
        """
        try:
            all_data = worksheet.get_all_values()
            if not all_data:
                return None

            game_data = {}
            game_name = worksheet.title

            # Проходим по всем строкам
            for row in all_data:
                # Пропускаем пустые строки
                if not row:
                    continue

                # Пропускаем строки-заголовки
                if row[0].strip() == "Поле" or row[0].strip() == "id":
                    continue

                key = row[0].strip() if len(row) > 0 else ""

                # Пропускаем пустые ключи
                if not key:
                    continue

                # Определяем значение (берём третью колонку, если есть)
                if len(row) >= 3:
                    value = row[2].strip() if row[2] else ""
                elif len(row) >= 2:
                    value = row[1].strip() if row[1] else ""
                else:
                    continue

                # Если ключ уже есть, сохраняем как список
                if key in game_data:
                    if isinstance(game_data[key], list):
                        if value:  # Добавляем только непустые значения
                            game_data[key].append(value)
                    else:
                        if value:  # Добавляем только непустые значения
                            game_data[key] = [game_data[key], value]
                elif value:  # Только непустые значения
                    game_data[key] = value

            # Фильтруем пустые значения
            game_data = {k: v for k, v in game_data.items() if v}

            return {
                "name": game_name,
                "data": game_data,
                "total_fields": len(game_data)
            }
        except Exception as e:
            print(f"❌ Ошибка чтения вкладки '{worksheet.title}': {e}")
            return None

    def read_service_sheet(self, worksheet):
        """
        Чтение служебной вкладки (с несколькими записями)
        """
        try:
            all_data = worksheet.get_all_values()
            if not all_data:
                return None

            headers = all_data[0] if all_data else []
            rows = all_data[1:]

            result = []
            for row in rows:
                # Проверяем, есть ли данные в строке
                has_data = any(cell.strip() for cell in row)
                if not has_data:
                    continue

                # Дополняем строку до длины заголовков
                while len(row) < len(headers):
                    row.append("")

                item = {}
                for i, header in enumerate(headers):
                    if header:
                        # Берём значение из соответствующей колонки
                        value = row[i].strip() if i < len(row) else ""
                        item[header] = value
                result.append(item)

            return result
        except Exception as e:
            print(
                f"❌ Ошибка чтения служебной вкладки '{worksheet.title}': {e}")
            return None

    def read_all_games(self):
        """Чтение всех игр и служебных данных"""
        if not self.spreadsheet:
            return {}, [], [], []

        all_games = {}
        updates = []
        addons = []
        knowledge_base = []

        for ws in self.get_all_sheets():
            title = ws.title

            if title in self.SERVICE_SHEETS:
                data = self.read_service_sheet(ws)
                if data:
                    if title == "📰 Новости по играм":
                        updates = data
                    elif title == "📦 Add-ons":
                        addons = data
                    elif title == "📚 KNOWLEDGE_BASE":
                        knowledge_base = data
            else:
                game = self.read_game_data(ws)
                if game and game['data']:
                    all_games[title] = game

        return all_games, updates, addons, knowledge_base

    def _get_value(self, data, key, default=""):
        """Безопасное получение значения из словаря"""
        value = data.get(key, default)
        if isinstance(value, list):
            return value[0] if value else default
        return value if value else default

    def format_game_info(self, game_name, game_data):
        """Форматирование информации об игре"""
        lines = []

        # Название
        lines.append(f"\n📌 {game_name}")
        lines.append("=" * 60)

        # Статус
        status = self._get_value(game_data, "status")
        status_icons = {
            "in_production": "🟢 В производстве",
            "preorder": "🟡 Предзаказ открыт",
            "active_preorder": "🟡 Предзаказ открыт",
            "delivery": "📦 В доставке/ожидании",
            "completed": "✅ Завершён",
            "on_hold": "🔴 Приостановлен"
        }
        status_display = status_icons.get(status, status or "Неизвестен")
        lines.append(f"📊 Статус: {status_display}")

        # Жанр
        genre = self._get_value(game_data, "genre")
        if genre:
            lines.append(f"🎮 Жанр: {genre}")

        # Игроки
        min_p = self._get_value(game_data, "min_players")
        max_p = self._get_value(game_data, "max_players")
        if min_p and max_p and min_p != "—":
            lines.append(f"👥 Игроков: {min_p}-{max_p}")
        elif min_p and min_p != "—":
            lines.append(f"👥 Игроков: от {min_p}")

        # Возраст
        age = self._get_value(game_data, "min_age")
        if age and age != "—":
            lines.append(f"🔞 Возраст: {age}+")

        # Время игры
        min_t = self._get_value(game_data, "play_time_min")
        max_t = self._get_value(game_data, "play_time_max")
        if min_t and max_t and min_t != "—":
            lines.append(f"⏱️ Время: {min_t}-{max_t} мин")
        elif min_t and min_t != "—":
            lines.append(f"⏱️ Время: {min_t} мин")

        # Цены
        prices = []
        preorder = self._get_value(game_data, "preorder_price")
        late = self._get_value(game_data, "Late_backer_price")
        base = self._get_value(game_data, "base_price")

        if preorder and preorder != "—":
            prices.append(f"Предзаказ: {preorder} ₽")
        if late and late != "—":
            prices.append(f"Поздние пташки: {late} ₽")
        if base and base != "—" and not preorder:
            prices.append(f"Цена: {base} ₽")

        if prices:
            lines.append(f"💰 {', '.join(prices)}")

        # Издатель
        publisher = self._get_value(game_data, "publisher")
        if publisher:
            lines.append(f"🏢 Издатель: {publisher}")

        # Локализация
        localization = self._get_value(game_data, "localization_status")
        if localization:
            lines.append(f"🌍 Локализация: {localization}")

        # Сложность
        complexity = self._get_value(game_data, "game_complexity")
        if complexity:
            lines.append(f"📊 Сложность: {complexity}")

        # Сроки
        deadline = self._get_value(game_data, "preorder_deadline")
        delivery = self._get_value(game_data, "delivery_estimate")
        if deadline:
            lines.append(f"📅 Предзаказ до: {deadline}")
        if delivery:
            lines.append(f"📦 Доставка: {delivery}")

        # Ссылки
        url = self._get_value(game_data, "source_url")
        if url:
            lines.append(f"🔗 {url}")

        # Бонусы
        bonus = self._get_value(game_data, "preorder_bonus")
        if bonus:
            lines.append(f"🎁 Бонусы: {bonus}")

        # Дополнительная информация
        additional = game_data.get("additional_info", "")
        if additional:
            if isinstance(additional, list):
                for item in additional:
                    if item:
                        lines.append(f"ℹ️ {item}")
            else:
                lines.append(f"ℹ️ {additional}")

        # Совместимость
        compatibility = self._get_value(game_data, "compatibility")
        if compatibility:
            lines.append(f"🔗 Совместимость: {compatibility}")

        # Описание
        description = game_data.get("description", "")
        if description:
            if isinstance(description, list):
                description = description[0] if description else ""
            description = description.strip('"')
            lines.append(f"\n📝 Описание:")
            for para in description.split('\n'):
                if para.strip():
                    lines.append(f"   {para.strip()}")

        # Содержимое
        contents = game_data.get("contents", "")
        if contents:
            if isinstance(contents, list):
                contents = contents[0] if contents else ""
            lines.append(f"\n📦 В комплекте:")
            for item in contents.split('\n'):
                if item.strip():
                    lines.append(f"   • {item.strip()}")

        # Дизайнеры/художники
        designers = self._get_value(game_data, "designers")
        artists = self._get_value(game_data, "artists")
        if designers:
            lines.append(f"\n🎨 Дизайнеры: {designers}")
        if artists:
            lines.append(f"🖌️ Художники: {artists}")

        # Язык
        language = self._get_value(game_data, "language")
        if language:
            lines.append(f"🌐 Язык: {language}")

        # Доступность
        available = self._get_value(game_data, "is_available")
        if available:
            lines.append(
                f"📦 Доступность: {'✅ Да' if available.lower() == 'да' else '❌ Нет'}")

        return "\n".join(lines)

    def format_game_short(self, game_name, game_data, index):
        """Краткое форматирование игры (одна строка)"""
        status_value = game_data.get("status", "")
        if isinstance(status_value, list):
            status = status_value[0] if status_value else ""
        else:
            status = status_value

        status_icons = {
            "in_production": "🟢",
            "preorder": "🟡",
            "active_preorder": "🟡",
            "delivery": "📦",
            "completed": "✅",
            "on_hold": "🔴"
        }
        icon = status_icons.get(status, "⚪")

        price = self._get_value(game_data, "preorder_price") or self._get_value(
            game_data, "base_price")
        if price and price != "—":
            return f"{index:>3}. {icon} {game_name} — {price} ₽"
        return f"{index:>3}. {icon} {game_name}"

    def print_games_by_status(self, games):
        """Вывод игр, сгруппированных по статусам"""
        if not games:
            print("📭 Игры не найдены")
            return

        groups = {
            "preorder": [],
            "active_preorder": [],
            "in_production": [],
            "delivery": [],
            "completed": [],
            "on_hold": [],
            "other": []
        }

        status_names = {
            "preorder": "🟡 Предзаказ открыт",
            "active_preorder": "🟡 Предзаказ открыт",
            "in_production": "🟢 В производстве",
            "delivery": "📦 В доставке/ожидании",
            "completed": "✅ Завершённые",
            "on_hold": "🔴 Приостановленные",
            "other": "📋 Другое"
        }

        for name, game in games.items():
            status_value = game['data'].get("status", "")
            if isinstance(status_value, list):
                status = status_value[0].strip(
                ).lower() if status_value else ""
            else:
                status = status_value.strip().lower() if status_value else ""

            if status in groups:
                groups[status].append((name, game))
            else:
                groups["other"].append((name, game))

        total = len(games)
        print(f"\n📚 Всего игр: {total}\n")

        for status_key, games_list in groups.items():
            if not games_list:
                continue

            count = len(games_list)
            percent = count / total * 100

            print(
                f"{status_names.get(status_key, status_key)} ({count} игр, {percent:.1f}%)")
            print("-" * 60)

            if count > 10:
                rows = []
                for i, (name, game) in enumerate(games_list, 1):
                    rows.append(self.format_game_short(name, game['data'], i))

                col_width = 40
                for i in range(0, len(rows), 3):
                    chunk = rows[i:i+3]
                    line = ""
                    for item in chunk:
                        line += item.ljust(col_width)
                    print(line)
            else:
                for i, (name, game) in enumerate(games_list, 1):
                    print(f"\n{i}. {name}")
                    data = game['data']

                    status_value = data.get("status", "")
                    if isinstance(status_value, list):
                        status = status_value[0] if status_value else ""
                    else:
                        status = status_value
                    print(f"   Статус: {status}")

                    price = self._get_value(
                        data, "preorder_price") or self._get_value(data, "base_price")
                    if price and price != "—":
                        print(f"   Цена: {price} ₽")

                    genre = self._get_value(data, "genre")
                    if genre:
                        print(f"   Жанр: {genre[:80]}...")

                    deadline = self._get_value(data, "preorder_deadline")
                    if deadline:
                        print(f"   Предзаказ до: {deadline}")

                    delivery = self._get_value(data, "delivery_estimate")
                    if delivery:
                        print(f"   Доставка: {delivery}")

            print()

    def print_updates(self, updates):
        """Вывод новостей"""
        if not updates:
            return

        print("\n" + "="*80)
        print("📰 НОВОСТИ ПО ПРОЕКТАМ")
        print("="*80)

        for update in updates[:5]:
            title = update.get("title", "")
            content = update.get("content", "")
            date = update.get("published_at", "")

            if title or content:
                print(f"\n📌 {title or 'Без заголовка'}")
                if date:
                    print(f"   📅 {date}")
                if content:
                    print(
                        f"   📝 {content[:300]}{'...' if len(content) > 300 else ''}")
                print("-" * 40)

    def print_addons(self, addons):
        """Вывод дополнений"""
        if not addons:
            return

        print("\n" + "="*80)
        print("📦 ДОПОЛНЕНИЯ (Add-ons)")
        print("="*80)

        active_addons = [a for a in addons if a.get(
            "Addon_is_active", "").lower() == "true"]
        inactive_addons = [a for a in addons if a.get(
            "Addon_is_active", "").lower() != "true"]

        if active_addons:
            print("\n🟢 Активные:")
            for addon in active_addons:
                name = addon.get("Addon_name", "")
                price = addon.get("Addon_price", "")
                if name:
                    print(f"   • {name}" + (f" — {price} ₽" if price else ""))

        if inactive_addons:
            print("\n🔴 Неактивные:")
            for addon in inactive_addons:
                name = addon.get("Addon_name", "")
                if name:
                    print(f"   • {name}")

    def print_knowledge_base(self, kb):
        """Вывод базы знаний"""
        if not kb:
            return

        print("\n" + "="*80)
        print("📚 БАЗА ЗНАНИЙ")
        print("="*80)

        active_items = [item for item in kb if item.get(
            "is_active", "").lower() == "true"]
        items_to_show = active_items if active_items else kb

        for item in items_to_show[:10]:
            question = item.get("question", "")
            content = item.get("content", "")
            content_type = item.get("content_type", "")

            if question:
                print(f"\n❓ {question}")
                if content:
                    print(
                        f"   💡 {content[:200]}{'...' if len(content) > 200 else ''}")
                if content_type:
                    print(f"   🏷️ Тип: {content_type}")
                print("-" * 40)

    def find_game(self, games, search_term):
        """Поиск игры по названию"""
        if not search_term or not games:
            return None

        clean_search = search_term.strip().lower()
        clean_search = clean_search.replace(
            '"', '').replace('«', '').replace('»', '')
        clean_search = clean_search.replace('(', '').replace(')', '')

        keywords = [k for k in clean_search.split() if len(k) > 1]

        matches = []
        for name, game in games.items():
            clean_name = name.lower()
            clean_name = clean_name.replace(
                '"', '').replace('«', '').replace('»', '')
            clean_name = clean_name.replace('(', '').replace(')', '')

            score = sum(1 for k in keywords if k in clean_name)
            if score > 0:
                status_value = game['data'].get("status", "")
                if isinstance(status_value, list):
                    status = status_value[0] if status_value else ""
                else:
                    status = status_value

                matches.append({
                    "name": name,
                    "game": game,
                    "score": score,
                    "status": status
                })

        if not matches:
            return None

        matches.sort(key=lambda x: x["score"], reverse=True)

        if len(matches) > 1:
            print(f"\n📋 Найдено несколько игр ({len(matches)}):")
            for i, match in enumerate(matches, 1):
                print(f"  {i}. {match['name']} (статус: {match['status']})")

            print("\nВыберите номер (Enter для первого):")
            choice = input("> ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(matches):
                return matches[int(choice) - 1]

        return matches[0]

    def export_to_json(self, games, updates, addons, knowledge_base, filename=DEFAULT_GAMES_DATA_PATH):
        """Экспорт данных в JSON"""
        try:
            data = {
                "exported_at": datetime.now().isoformat(),
                "total_games": len(games),
                "games": {},
                "updates": updates,
                "addons": addons,
                "knowledge_base": knowledge_base
            }

            for name, game in games.items():
                data["games"][name] = game['data']

            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)

            print(f"\n✅ Данные сохранены в {filename}")
            return True
        except Exception as e:
            print(f"❌ Ошибка сохранения: {e}")
            return False

# ==================== ОСНОВНАЯ ФУНКЦИЯ ====================


def main():
    print("\n" + "="*80)
    print("🔄 ЗАПУСК ЧТЕНИЯ ИГР ИЗ GOOGLE SHEETS")
    print("="*80)

    reader = SheetsReader()

    if not reader.client:
        print("\n❌ Не удалось подключиться. Проверьте:")
        print("   1. Файл credentials.json в папке проекта")
        print("   2. Правильность GOOGLE_SHEETS_ID в .env")
        return

    # Читаем все данные
    games, updates, addons, knowledge_base = reader.read_all_games()

    if not games:
        print("❌ Игры не найдены")
        return

    # Сохраняем в JSON
    reader.export_to_json(games, updates, addons, knowledge_base)

    # Выводим список всех игр
    print("\n" + "="*80)
    print("📊 ВСЕ ИГРЫ")
    print("="*80)

    for name in games.keys():
        print(f"   📌 {name}")

    # Выводим игры по статусам
    reader.print_games_by_status(games)

    # Выводим новости
    reader.print_updates(updates)

    # Выводим дополнения
    reader.print_addons(addons)

    # Выводим базу знаний
    reader.print_knowledge_base(knowledge_base)

    # ==========================================
    # ИНТЕРАКТИВНЫЙ ПОИСК (бесконечный цикл)
    # ==========================================
    print("\n" + "="*80)
    print("🔍 ИНТЕРАКТИВНЫЙ ПОИСК ИГР")
    print("="*80)
    print("Введите название игры для поиска")
    print("Команды:")
    print("  • /list  - показать все игры")
    print("  • /stats - показать статистику по статусам")
    print("  • /exit  - выйти из поиска")
    print("="*80)

    while True:
        print("\n🔎 Введите название игры (или команду):")
        search_term = input("> ").strip()

        # Проверяем команды
        if search_term.lower() in ["/exit", "/quit", "exit", "quit", "выход"]:
            print("👋 До свидания!")
            break

        if search_term.lower() == "/list":
            print("\n📋 ВСЕ ИГРЫ:")
            for i, name in enumerate(games.keys(), 1):
                print(f"   {i}. {name}")
            continue

        if search_term.lower() == "/stats":
            reader.print_games_by_status(games)
            continue

        if not search_term:
            print("⚠️ Введите название игры или команду")
            continue

        # Поиск игры
        found = reader.find_game(games, search_term)
        if found:
            print("\n" + "="*80)
            print(reader.format_game_info(
                found['name'], found['game']['data']))
            print("="*80)

            # Предлагаем показать новости и дополнения для этой игры
            print("\n📌 Показать связанные данные?")
            print("   [1] Показать новости по игре")
            print("   [2] Показать дополнения")
            print("   [3] Показать всё")
            print("   [Enter] Пропустить")

            choice = input("> ").strip()

            if choice == "1" or choice == "3":
                # Показываем новости, связанные с игрой
                game_updates = [u for u in updates if search_term.lower() in u.get(
                    "title", "").lower() or search_term.lower() in u.get("content", "").lower()]
                if game_updates:
                    print("\n📰 НОВОСТИ ПО ИГРЕ:")
                    for update in game_updates[:3]:
                        print(f"   📌 {update.get('title', 'Без заголовка')}")
                        if update.get("published_at"):
                            print(f"      📅 {update.get('published_at')}")
                        if update.get("content"):
                            print(f"      📝 {update.get('content')[:150]}...")
                        print()
                else:
                    print("📭 Новостей по этой игре не найдено")

            if choice == "2" or choice == "3":
                # Показываем дополнения
                active_addons = [a for a in addons if a.get(
                    "Addon_is_active", "").lower() == "true"]
                if active_addons:
                    print("\n📦 ДОПОЛНЕНИЯ:")
                    for addon in active_addons[:5]:
                        name = addon.get("Addon_name", "")
                        price = addon.get("Addon_price", "")
                        if name:
                            print(f"   • {name}" +
                                  (f" — {price} ₽" if price else ""))
                else:
                    print("📭 Активных дополнений нет")
        else:
            print(f"❌ Игра '{search_term}' не найдена")
            print("💡 Попробуйте ввести часть названия или проверьте написание")

            # Предлагаем показать похожие игры
            similar = []
            for name in games.keys():
                if any(word in name.lower() for word in search_term.lower().split() if len(word) > 2):
                    similar.append(name)

            if similar:
                print(f"\n📋 Возможно, вы искали:")
                for name in similar[:5]:
                    print(f"   • {name}")
                if len(similar) > 5:
                    print(f"   ... и ещё {len(similar) - 5}")


if __name__ == "__main__":
    main()
    print("\n✅ Готово!")
