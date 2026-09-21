import re


class CommentNormalizer:
    """Нормализация текста комментариев и постов"""

    @staticmethod
    def normalize_text(text: str, remove_emoji: bool = False) -> str:
        if not text:
            return ""

        # Ссылки
        text = re.sub(r'https?://\S+|www\.\S+', '[ссылка]', text)
        # Упоминания
        text = re.sub(r'@\w+', '[упоминание]', text)
        # Хештеги
        text = re.sub(r'#\w+', '[хештег]', text)
        # Множественные пробелы
        text = re.sub(r'\s+', ' ', text)

        if remove_emoji:
            emoji_pattern = re.compile(
                "["
                u"\U0001F600-\U0001F64F"
                u"\U0001F300-\U0001F5FF"
                u"\U0001F680-\U0001F6FF"
                u"\U0001F700-\U0001F77F"
                u"\U0001F780-\U0001F7FF"
                u"\U0001F800-\U0001F8FF"
                u"\U0001F900-\U0001F9FF"
                u"\U0001FA00-\U0001FA6F"
                u"\U0001FA70-\U0001FAFF"
                u"\U00002702-\U000027B0"
                u"\U000024C2-\U0001F251"
                "]+",
                flags=re.UNICODE
            )
            text = emoji_pattern.sub('', text)

        return text.strip()

    @staticmethod
    def normalize_post(post_data: dict) -> dict:
        original = post_data.get('text', '')
        normalized = CommentNormalizer.normalize_text(original)
        post_data['text_original'] = original
        post_data['text_normalized'] = normalized
        post_data['text'] = normalized  # перезаписываем для единообразия
        return post_data

    @staticmethod
    def normalize_comment(comment_data: dict) -> dict:
        original = comment_data.get('text', '')
        normalized = CommentNormalizer.normalize_text(original)
        comment_data['text_original'] = original
        comment_data['text_normalized'] = normalized
        comment_data['text'] = normalized
        return comment_data
