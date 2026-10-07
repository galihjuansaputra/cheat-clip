import re
from typing import List, Optional

def parse_time_str(time_str: str) -> float:
    """Parses time string in formats like HH:MM:SS,mmm or MM:SS,mmm or HH:MM:SS or MM:SS to seconds."""
    time_str = time_str.strip().replace(',', '.')
    # Extract millisecond if present
    ms = 0.0
    if '.' in time_str:
        parts = time_str.split('.')
        time_str = parts[0]
        try:
            ms = float('0.' + parts[1])
        except ValueError:
            pass
            
    time_parts = time_str.split(':')
    try:
        if len(time_parts) == 3:
            return int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2]) + ms
        elif len(time_parts) == 2:
            return int(time_parts[0]) * 60 + int(time_parts[1]) + ms
        elif len(time_parts) == 1:
            return float(time_parts[0]) + ms
    except ValueError:
        return 0.0

def parse_manual_subtitles(content: str, default_duration: float = 0.0) -> List[dict]:
    # Normalize line endings
    content = content.replace('\r\n', '\n').strip()
    
    # 1. Try standard SRT parsing first
    srt_regex = r'(?:\d+\n)?(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\n|\n\d+\n|\Z)'
    srt_matches = re.findall(srt_regex, content, re.DOTALL)
    
    if srt_matches:
        results = []
        for start_str, end_str, text in srt_matches:
            start = parse_time_str(start_str)
            end = parse_time_str(end_str)
            cleaned_text = text.replace('\n', ' ').strip()
            results.append({
                "text": cleaned_text,
                "start": start,
                "duration": max(0.1, end - start)
            })
        if results:
            return results

    # 2. Try parsing line-by-line for timestamped lines
    line_time_range_regex = r'^[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)\s*(?:-|-->|\s)\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)[\]\)]?\s*(.*)'
    line_single_time_regex = r'^[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)[\]\)]?\s*(.*)'
    
    lines = content.split('\n')
    results = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Match range first (e.g. 00:12 - 00:15 Text)
        m_range = re.match(line_time_range_regex, line)
        if m_range:
            start = parse_time_str(m_range.group(1))
            end = parse_time_str(m_range.group(2))
            text = m_range.group(3).strip()
            results.append({
                "text": text,
                "start": start,
                "duration": max(0.1, end - start)
            })
            continue
            
        # Match single timestamp (e.g. 00:12 Text)
        m_single = re.match(line_single_time_regex, line)
        if m_single:
            start = parse_time_str(m_single.group(1))
            text = m_single.group(2).strip()
            results.append({
                "text": text,
                "start": start,
                "duration": -1.0  # Will fill in later
            })
            continue

    if results:
        # Resolve duration for single timestamps
        for i in range(len(results)):
            if results[i]["duration"] == -1.0:
                if i + 1 < len(results):
                    diff = results[i+1]["start"] - results[i]["start"]
                    results[i]["duration"] = max(0.5, diff)
                else:
                    results[i]["duration"] = 3.0  # default for the last line
        return results

    # 3. Fallback: split text into paragraphs or sentences and distribute evenly across video duration
    duration_to_use = default_duration if default_duration > 0 else 60.0
    raw_sentences = [s.strip() for s in re.split(r'(?<=[.?!])\s+|\n+', content) if s.strip()]
    if raw_sentences:
        num_sentences = len(raw_sentences)
        sec_per_sentence = duration_to_use / num_sentences
        results = []
        for i, text in enumerate(raw_sentences):
            start = i * sec_per_sentence
            results.append({
                "text": text,
                "start": round(start, 2),
                "duration": round(sec_per_sentence, 2)
            })
        return results
        
    return []

def extract_video_id(url: str) -> Optional[str]:
    """Extracts the 11-character YouTube video ID from various URL formats including live streams, shorts, embed, watch?v=, youtu.be, etc."""
    if not url:
        return None
    trimmed = url.strip()
    if re.match(r"^[a-zA-Z0-9_-]{11}$", trimmed):
        return trimmed

    patterns = [
        r"(?:[?&]v=)([a-zA-Z0-9_-]{11})",
        r"(?:youtu\.be\/|(?:www\.|m\.)?youtube(?:-nocookie)?\.com\/(?:embed|v|shorts|live)\/)([a-zA-Z0-9_-]{11})",
        r"(?:v=|\/v\/|embed\/|shorts\/|live\/|youtu\.be\/|\/embed\/|\/watch\?v=|\/watch\?.+&v=)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, trimmed)
        if match:
            return match.group(1)

    return None

def lowercase_hashtags_in_string(text: str) -> str:
    """Finds all hashtags (#word) in a string and converts them to lowercase."""
    if not text:
        return text
    return re.sub(r'#\w+', lambda m: m.group(0).lower(), text)

LANGUAGE_NAMES = {
    'id': 'Indonesian (Bahasa Indonesia)',
    'en': 'English',
    'es': 'Spanish (Español)',
    'pt': 'Portuguese (Português)',
    'fr': 'French (Français)',
    'de': 'German (Deutsch)',
    'ja': 'Japanese (日本語)',
    'ko': 'Korean (한국어)',
    'zh': 'Chinese (中文)',
    'ar': 'Arabic (العربية)',
    'ru': 'Russian (Русский)',
}

ID_STOPWORDS = {
    # Pronouns & People
    'saya', 'aku', 'gue', 'gua', 'gw', 'kamu', 'lu', 'lo', 'elu', 'kita', 'kami', 'mereka',
    'dia', 'beliau', 'kalian', 'orang', 'bang', 'mas', 'mbak', 'kak', 'kakak', 'bro', 'sis',
    'pak', 'bapak', 'bu', 'ibu', 'om', 'tante', 'anak', 'teman', 'temen', 'gais', 'guys',
    # Connectives, Prepositions & Articles
    'yang', 'dan', 'di', 'ke', 'dari', 'pada', 'dalam', 'untuk', 'utk', 'dengan', 'dgn',
    'sama', 'karena', 'krn', 'sebab', 'oleh', 'bagi', 'antara', 'tentang', 'seperti', 'kayak',
    'kalo', 'kalau', 'klo', 'jika', 'apabila', 'tapi', 'tp', 'tetapi', 'namun', 'melainkan',
    'walaupun', 'meskipun', 'supaya', 'agar', 'atau', 'maupun', 'serta', 'yaitu', 'yakni',
    # Particles & Conversational Markers (unique to Indonesian speech)
    'ya', 'nih', 'tuh', 'dong', 'kan', 'lah', 'deh', 'sih', 'kok', 'loh', 'lho', 'kah', 'pun',
    'nggak', 'gak', 'ngga', 'ga', 'tak', 'bukan', 'jangan', 'udah', 'udh', 'sudah', 'belum', 'blm',
    'banget', 'bgt', 'aja', 'saja', 'doang', 'cuma', 'cuman', 'hanya', 'sangat', 'amat',
    'gitu', 'gini', 'begitu', 'begini', 'makanya', 'makannya', 'padahal', 'emang', 'memang',
    'bener', 'beneran', 'benar', 'pasti', 'bisa', 'gabisa', 'dapat', 'boleh', 'harus', 'hrs',
    'mau', 'ingin', 'bakal', 'akan', 'lagi', 'lg', 'sedang', 'masih', 'msh', 'terus', 'trus',
    'dulu', 'dl', 'kemarin', 'besok', 'nanti', 'sekarang', 'skrg', 'saat', 'ketika', 'waktu', 'pas',
    # Question words
    'apa', 'apakah', 'siapa', 'kenapa', 'knp', 'mengapa', 'gimana', 'bagaimana', 'dimana', 'kapan',
    # Common speech verbs & concepts
    'jadi', 'adalah', 'ada', 'punya', 'tau', 'tahu', 'tonton', 'nonton', 'bilang', 'katanya',
    'kata', 'ngomong', 'bikin', 'buat', 'liat', 'lihat', 'pikir', 'kira', 'rasa', 'rasanya',
    'menurut', 'maksud', 'maksudnya', 'paham', 'ngerti', 'banyak', 'kurang', 'lebih', 'semua',
    'sendiri', 'lain', 'lainnya', 'biasa', 'luar', 'soal', 'soalnya', 'masalah', 'cerita'
}

EN_STOPWORDS = {
    'the', 'and', 'to', 'of', 'a', 'in', 'that', 'is', 'it', 'you', 'for', 'on',
    'are', 'as', 'with', 'they', 'at', 'be', 'this', 'have', 'from', 'or', 'one',
    'had', 'by', 'but', 'not', 'what', 'all', 'were', 'we', 'when', 'your', 'can',
    'there', 'an', 'which', 'she', 'do', 'how', 'their', 'if', 'will', 'up', 'about',
    'out', 'so', 'would', 'like', 'just', 'know', 'people', 'think', 'going', 'been',
    'them', 'some', 'could', 'him', 'into', 'other', 'than', 'then', 'now', 'look',
    'only', 'come', 'its', 'over', 'also', 'back', 'after', 'use', 'two', 'our',
    'work', 'first', 'well', 'way', 'even', 'new', 'want', 'because', 'any', 'these',
    'give', 'day', 'most', 'us', 'time', 'really', 'something', 'good', 'make',
    'gonna', 'wanna', 'gotta', 'yeah', 'yes', 'no', 'okay', 'right', 'here', 'why',
    'where', 'who', 'should', 'must', 'said', 'say', 'tell', 'told', 'see', 'saw',
    'much', 'many', 'very', 'still', 'never', 'always', 'today', 'tomorrow', 'yesterday'
}

ES_STOPWORDS = {
    'de', 'la', 'que', 'el', 'en', 'y', 'a', 'los', 'del', 'se', 'las', 'por',
    'un', 'para', 'con', 'no', 'una', 'su', 'al', 'lo', 'como', 'más', 'pero',
    'sus', 'le', 'ya', 'o', 'este', 'sí', 'porque', 'esta', 'son', 'entre',
    'está', 'cuando', 'muy', 'sin', 'sobre', 'ser', 'tiene', 'también', 'me',
    'hasta', 'hay', 'donde', 'quien', 'desde', 'todo', 'nos', 'durante', 'todos',
    'uno', 'les', 'ni', 'contra', 'otros', 'ese', 'eso', 'ante', 'ellos', 'esto'
}

PT_STOPWORDS = {
    'de', 'a', 'o', 'que', 'e', 'do', 'da', 'em', 'um', 'para', 'é', 'com',
    'não', 'uma', 'os', 'no', 'se', 'na', 'por', 'mais', 'as', 'dos', 'como',
    'mas', 'foi', 'ao', 'ele', 'das', 'tem', 'à', 'seu', 'sua', 'ou', 'ser',
    'quando', 'muito', 'há', 'nos', 'já', 'está', 'eu', 'também', 'só', 'pelo',
    'pela', 'você', 'isso', 'ela', 'entre', 'depois'
}

FR_STOPWORDS = {
    'de', 'la', 'le', 'et', 'les', 'des', 'en', 'un', 'du', 'une', 'que', 'est',
    'pour', 'qui', 'dans', 'a', 'par', 'plus', 'pas', 'au', 'sur', 'ne', 'se',
    'ce', 'il', 'sont', 'avec', 'son', 'cette', 'aux', 'ses', 'mais', 'ou',
    'ont', 'tout', 'comme', 'nous', 'sa', 'vous'
}

DE_STOPWORDS = {
    'der', 'die', 'und', 'in', 'den', 'von', 'zu', 'das', 'mit', 'sich', 'des',
    'auf', 'für', 'ist', 'im', 'dem', 'nicht', 'ein', 'eine', 'als', 'auch',
    'es', 'an', 'werden', 'aus', 'er', 'hat', 'dass', 'sie', 'nach', 'wird',
    'bei', 'einer', 'um', 'am', 'sind', 'noch', 'wie', 'einem', 'über'
}

def detect_transcript_language(transcript_lines: List[dict], title: str = "") -> dict:
    """
    Detects the primary spoken language of the video transcript with primary focus on
    Indonesian (Bahasa Indonesia) and English.
    
    Prevents false Arabic/exotic classifications when Indonesian videos contain occasional
    Arabic greetings (e.g. Assalamu'alaikum, Bismillah) or Islamic quotes.
    
    Returns dict: {'code': 'id'|'en'|..., 'name': str, 'confidence': float}
    """
    if not transcript_lines and not title:
        return {'code': 'en', 'name': 'English', 'confidence': 0.5}

    sample_texts = [title] if title else []
    for line in (transcript_lines[:250] if transcript_lines else []):
        t = line.get("text", "")
        if t:
            sample_texts.append(t)
    
    full_sample = " ".join(sample_texts).strip()
    if not full_sample:
        return {'code': 'en', 'name': 'English', 'confidence': 0.5}

    # Extract Latin words
    tokens = re.findall(r'\b[a-zA-Z\u00C0-\u024F\u1E00-\u1EFF]+\b', full_sample.lower())
    title_tokens = set(re.findall(r'\b[a-zA-Z]+\b', title.lower())) if title else set()

    # 1. Primary Check: Score Indonesian & English
    # Indonesian grammatical prefixes / suffixes pattern
    id_morphology_matches = sum(
        1 for w in tokens
        if len(w) >= 5 and (
            w.startswith(('meng', 'men', 'mem', 'peng', 'pen', 'pem', 'ber', 'ter', 'di')) or
            w.endswith(('kan', 'nya', 'lah', 'kah', 'pun'))
        )
    )

    id_count = sum(1 for w in tokens if w in ID_STOPWORDS) + (id_morphology_matches // 3)
    en_count = sum(1 for w in tokens if w in EN_STOPWORDS)

    # Title hints
    title_id = sum(1 for w in title_tokens if w in ID_STOPWORDS)
    title_en = sum(1 for w in title_tokens if w in EN_STOPWORDS)
    id_count += title_id * 2
    en_count += title_en * 2

    # 2. Strict Check: Only trigger Non-Latin scripts if they constitute DOMINANT text
    all_letters = re.findall(r'[\w]', full_sample)
    total_letters = len(all_letters) if all_letters else 1

    arabic_chars = len(re.findall(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', full_sample))
    ja_chars = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF]', full_sample))
    ko_chars = len(re.findall(r'[\uAC00-\uD7AF\u1100-\u11FF]', full_sample))
    zh_chars = len(re.findall(r'[\u4E00-\u9FFF]', full_sample))
    ru_chars = len(re.findall(r'[\u0400-\u04FF]', full_sample))

    # If Arabic is just an Islamic greeting or snippet in an Indonesian video, DO NOT classify as Arabic!
    if arabic_chars / total_letters > 0.45 and id_count == 0 and en_count <= 1:
        return {'code': 'ar', 'name': LANGUAGE_NAMES['ar'], 'confidence': 0.95}
    if ja_chars / total_letters > 0.35 and id_count == 0 and en_count <= 1:
        return {'code': 'ja', 'name': LANGUAGE_NAMES['ja'], 'confidence': 0.95}
    if ko_chars / total_letters > 0.35 and id_count == 0 and en_count <= 1:
        return {'code': 'ko', 'name': LANGUAGE_NAMES['ko'], 'confidence': 0.95}
    if zh_chars / total_letters > 0.35 and id_count == 0 and en_count <= 1:
        return {'code': 'zh', 'name': LANGUAGE_NAMES['zh'], 'confidence': 0.95}
    if ru_chars / total_letters > 0.35 and id_count == 0 and en_count <= 1:
        return {'code': 'ru', 'name': LANGUAGE_NAMES['ru'], 'confidence': 0.95}

    # 3. Decision between Bahasa Indonesia and English (First-class priority)
    # Note: Indonesian tech/gaming creators use frequent English loanwords (e.g. 'guys', 'game', 'video', 'content').
    # Therefore, if there are distinctive Indonesian markers present, it is Indonesian!
    if id_count >= 2 and (id_count >= en_count * 0.4 or title_id > 0):
        confidence = round(min(0.98, max(0.65, id_count / max(1, id_count + en_count))), 2)
        return {
            'code': 'id',
            'name': LANGUAGE_NAMES['id'],
            'confidence': confidence
        }

    if en_count >= 3 and (en_count > id_count * 1.5 or title_en > 0):
        confidence = round(min(0.98, max(0.65, en_count / max(1, id_count + en_count))), 2)
        return {
            'code': 'en',
            'name': LANGUAGE_NAMES['en'],
            'confidence': confidence
        }

    # 4. Check other European Latin languages only if they substantially beat both ID and EN
    other_counts = {
        'es': sum(1 for w in tokens if w in ES_STOPWORDS),
        'pt': sum(1 for w in tokens if w in PT_STOPWORDS),
        'fr': sum(1 for w in tokens if w in FR_STOPWORDS),
        'de': sum(1 for w in tokens if w in DE_STOPWORDS),
    }
    best_other_lang, best_other_score = max(other_counts.items(), key=lambda item: item[1])
    if best_other_score >= 6 and best_other_score > (id_count * 2) and best_other_score > (en_count * 1.5):
        return {
            'code': best_other_lang,
            'name': LANGUAGE_NAMES.get(best_other_lang, best_other_lang.upper()),
            'confidence': 0.85
        }

    # 5. Default fallback: Compare ID vs EN directly
    if id_count > en_count or title_id > title_en:
        return {'code': 'id', 'name': LANGUAGE_NAMES['id'], 'confidence': 0.70}
    
    return {'code': 'en', 'name': LANGUAGE_NAMES['en'], 'confidence': 0.70}

def sanitize_first_person_title(title: str, speaker_or_channel: str = "", lang: str = "en") -> str:
    """
    Sanitizes accidental first-person perspective ('I', 'Me', 'My', 'Saya', 'Aku', 'Gue')
    from generated clip titles and title suggestions, replacing them with speaker or channel attribution,
    or objective framing so titles never appear as the user's personal opinion.
    Preserves the target language (Indonesian, English, Spanish, etc.).
    """
    if not title:
        return title
    t = title.strip()
    speaker = speaker_or_channel.strip() if speaker_or_channel else ""
    if lang == "id":
        subject = speaker if speaker else "Host"
    elif lang == "es":
        subject = speaker if speaker else "El Presentador"
    else:
        subject = speaker if speaker else "The Speaker"

    # 1. English Why / How / What / When / Where
    t = re.sub(r"^why\s+i\s+think\b", f"{subject} Explains Why", t, flags=re.IGNORECASE)
    t = re.sub(r"^why\s+i\s+believe\b", f"{subject} Explains Why", t, flags=re.IGNORECASE)
    t = re.sub(r"^why\s+i\s+", f"Why {subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^how\s+i\s+", f"How {subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^what\s+i\s+think\b", f"{subject}'s Thoughts On", t, flags=re.IGNORECASE)
    t = re.sub(r"^what\s+i\s+learned\b", f"What {subject} Learned", t, flags=re.IGNORECASE)
    t = re.sub(r"^what\s+i\s+", f"What {subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^when\s+i\s+", f"When {subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^where\s+i\s+", f"Where {subject} ", t, flags=re.IGNORECASE)

    # 2. English My [Noun] (e.g. My Opinion, My Story, My Regret)
    t = re.sub(r"^my\s+([a-zA-Z]+)", lambda m: f"{subject}'s {m.group(1)}" if speaker else f"The {m.group(1)}", t, flags=re.IGNORECASE)

    # 3. English First-person action verbs (e.g. I Tried, I Discovered, I Built)
    t = re.sub(r"^i\s+(tried|found|made|discovered|bought|quit|lost|learned|realized|spent|built|saw|went|started|joined|left|hate|love)\b", rf"{subject} \1", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+was\b", f"{subject} Was", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+am\b", f"{subject} Is", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+have\b", f"{subject} Has", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+had\b", f"{subject} Had", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+got\b", f"{subject} Got", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+think\b", f"{subject} Thinks", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+believe\b", f"{subject} Believes", t, flags=re.IGNORECASE)

    # 4. Indonesian / Malay first-person replacements (Saya, Aku, Gue, Gw)
    indo_subject = speaker if speaker else "Host"
    t = re.sub(r"^(kenapa|mengapa)\s+(saya|aku|gue|gw)\s+", rf"\1 {indo_subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^(cara|bagaimana)\s+(saya|aku|gue|gw)\s+", rf"Cara {indo_subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^(alasan)\s+(saya|aku|gue|gw)\s+", rf"Alasan {indo_subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^(saya|aku|gue|gw)\s+(mencoba|menemukan|membuat|yakin|berpikir|menyesal|kehilangan|belajar|mulai|berhenti)\b", rf"{indo_subject} \2", t, flags=re.IGNORECASE)
    t = re.sub(r"^(pendapat|opini)\s+(saya|aku|gue|gw)\b", rf"Opini {indo_subject}", t, flags=re.IGNORECASE)

    return t.strip()


def is_safe_remote_url(url: str, allowed_domains: Optional[set] = None) -> bool:
    """
    Validates if a URL is safe to fetch remotely, preventing SSRF attacks to
    local loopback, private IP subnets, or cloud metadata services.
    """
    if not url:
        return False
    u = url.strip()
    try:
        import ipaddress
        import urllib.parse

        parsed = urllib.parse.urlsplit(u)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower().strip()
        if not hostname:
            return False

        # Block localhost / link-local / loopback hostnames
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "metadata.google.internal"):
            return False

        # Check if hostname is an IP address and verify if private/reserved
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        except ValueError:
            # Not an IP literal, it's a domain name
            pass

        if allowed_domains:
            if not any(hostname == d or hostname.endswith("." + d) for d in allowed_domains):
                return False

        return True
    except Exception:
        return False
