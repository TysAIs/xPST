"""AI caption generation from video transcripts.

Uses the KB's LLM client (when configured) to generate platform-specific
caption suggestions from a transcript. Falls back to deterministic extraction
(hashtag + keyword-based) when no LLM is configured.

Platform-specific formatting:
- X: 280 chars max
- Threads: 500 chars max
- Instagram: 2200 chars max
- LinkedIn: 3000 chars max
- YouTube: 5000 chars max (title + description)
- TikTok: 2200 chars max
"""

from __future__ import annotations

import re
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Platform character limits
PLATFORM_CHAR_LIMITS: dict[str, int] = {
    "x": 280,
    "threads": 500,
    "instagram": 2200,
    "linkedin": 3000,
    "youtube": 5000,
    "tiktok": 2200,
}

# Common hashtags by topic keyword
HASHTAG_SUGGESTIONS: dict[str, list[str]] = {
    "ai": ["#AI", "#ArtificialIntelligence", "#MachineLearning", "#TechTrends"],
    "tech": ["#Tech", "#Technology", "#Innovation", "#TechTips"],
    "business": ["#Business", "#Entrepreneur", "#Startup", "#BusinessTips"],
    "marketing": ["#Marketing", "#DigitalMarketing", "#ContentCreation", "#SocialMedia"],
    "education": ["#Education", "#Learning", "#Tutorial", "#HowTo"],
    "finance": ["#Finance", "#Investing", "#MoneyTips", "#FinancialLiteracy"],
    "health": ["#Health", "#Wellness", "#Fitness", "#HealthyLiving"],
    "gaming": ["#Gaming", "#Gamer", "#Gameplay", "#GamingCommunity"],
    "music": ["#Music", "#NewMusic", "#Musician", "#MusicVideo"],
    "food": ["#Food", "#Cooking", "#Recipe", "#Foodie"],
}


def generate_caption_deterministic(
    transcript_text: str,
    platform: str = "instagram",
    max_hashtags: int = 5,
) -> list[dict[str, str]]:
    """Generate caption suggestions without an LLM (deterministic fallback).

    Extracts key sentences, keywords, and suggested hashtags from the transcript.

    Returns:
        List of 3 caption variants, each with "caption" and "hashtags" keys.
    """
    # Split into sentences
    sentences = re.split(r"[.!?]+", transcript_text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]

    if not sentences:
        return [
            {"caption": "Check out this video!", "hashtags": "#video #content"},
            {"caption": "New content alert!", "hashtags": "#new #content"},
            {"caption": "Watch this!", "hashtags": "#watch #video"},
        ]

    # Extract keywords for hashtags
    words = re.findall(r"\b[a-zA-Z]{4,}\b", transcript_text.lower())
    word_freq: dict[str, int] = {}
    for w in words:
        word_freq[w] = word_freq.get(w, 0) + 1
    top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:10]

    # Match keywords to hashtag suggestions
    suggested_hashtags: list[str] = []
    for word, _ in top_words:
        if word in HASHTAG_SUGGESTIONS:
            suggested_hashtags.extend(HASHTAG_SUGGESTIONS[word][:2])
        else:
            suggested_hashtags.append(f"#{word.capitalize()}")

    # Deduplicate and limit
    seen: set[str] = set()
    unique_hashtags: list[str] = []
    for tag in suggested_hashtags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            unique_hashtags.append(tag)
    unique_hashtags = unique_hashtags[:max_hashtags]

    char_limit = PLATFORM_CHAR_LIMITS.get(platform, 2200)

    # Variant 1: First compelling sentence + hashtags
    caption1 = sentences[0][:char_limit - 200].strip()
    if len(sentences) > 1 and len(caption1) < char_limit // 2:
        caption1 += f" {sentences[1][:100].strip()}"
    hashtag_str = " ".join(unique_hashtags[:3])
    caption1_full = f"{caption1}\n\n{hashtag_str}"
    if len(caption1_full) > char_limit:
        caption1_full = caption1_full[:char_limit]

    # Variant 2: Question-based hook
    question_starters = [
        "Did you know?", "Here's the thing:", "Wait for it...",
        "This changed everything:", "You need to see this:",
    ]
    starter = question_starters[len(sentences) % len(question_starters)]
    caption2 = f"{starter} {sentences[min(1, len(sentences)-1)][:char_limit - 200].strip()}"
    hashtag_str2 = " ".join(unique_hashtags[:4])
    caption2_full = f"{caption2}\n\n{hashtag_str2}"
    if len(caption2_full) > char_limit:
        caption2_full = caption2_full[:char_limit]

    # Variant 3: Summary + call to action
    summary = sentences[-1][:char_limit // 3].strip() if sentences else "Watch now!"
    caption3 = f"{summary}\n\nFollow for more! {' '.join(unique_hashtags)}"
    if len(caption3) > char_limit:
        caption3 = caption3[:char_limit]

    return [
        {"caption": caption1_full, "hashtags": hashtag_str, "style": "direct"},
        {"caption": caption2_full, "hashtags": hashtag_str2, "style": "hook"},
        {"caption": caption3, "hashtags": " ".join(unique_hashtags), "style": "summary"},
    ]


async def generate_caption_llm(
    transcript_text: str,
    platform: str = "instagram",
    llm_config: Any = None,
) -> list[dict[str, str]]:
    """Generate caption suggestions using an LLM.

    Requires a configured LLM endpoint. Falls back to deterministic if LLM
    is not available.

    Returns:
        List of 3 caption variants with "caption", "hashtags", and "style" keys.
    """
    char_limit = PLATFORM_CHAR_LIMITS.get(platform, 2200)

    try:
        from xpst.knowledge.llm.client import LLMClient

        if llm_config is None:
            from xpst.knowledge.config import KnowledgeConfig
            llm_config = KnowledgeConfig.from_env()

        if not llm_config.llm_enabled or not llm_config.llm_base_url:
            logger.debug("LLM not configured, falling back to deterministic captions")
            return generate_caption_deterministic(transcript_text, platform)

        client = LLMClient(
            base_url=llm_config.llm_base_url,
            model=llm_config.llm_model or "gpt-4o-mini",
        )

        prompt = (
            f"Generate 3 engaging caption variants for a {platform} video.\n"
            f"Platform char limit: {char_limit}\n"
            f"Transcript: {transcript_text[:2000]}\n\n"
            f"Return 3 captions as JSON: [{{\"caption\": \"...\", \"hashtags\": \"#tag1 #tag2\", \"style\": \"direct|hook|summary\"}}]"
        )

        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.chat_json([{"role": "user", "content": prompt}]),
        )
        # chat_json returns a dict; if it's a list, use directly
        if isinstance(result, list):
            captions = result
        elif isinstance(result, dict) and "captions" in result:
            captions = result["captions"]
        else:
            captions = [result]
        return captions[:3]

    except Exception as e:
        logger.debug(f"LLM caption generation failed, using deterministic: {e}")
        return generate_caption_deterministic(transcript_text, platform)


def generate_caption(
    transcript_text: str,
    platform: str = "instagram",
    llm_config: Any = None,
) -> list[dict[str, str]]:
    """Generate caption suggestions (synchronous wrapper).

    Uses LLM if configured, otherwise falls back to deterministic extraction.

    Args:
        transcript_text: The video transcript text.
        platform: Target platform for character limit formatting.
        llm_config: Optional KnowledgeConfig for LLM settings.

    Returns:
        List of 3 caption variants with "caption", "hashtags", and "style" keys.
    """
    import asyncio

    try:
        return asyncio.run(generate_caption_llm(transcript_text, platform, llm_config))
    except RuntimeError:
        # Already in an event loop
        return generate_caption_deterministic(transcript_text, platform)


# ── Post idea generation ────────────────────────────────────────────────

# Engagement-tested idea templates (rotated per slot so consecutive ideas
# never share a hook shape). "{topic}" is substituted at call time.
IDEA_TEMPLATES: tuple[str, ...] = (
    "5 things nobody tells you about {topic}",
    "The {topic} mistake that costs you the most",
    "How I turned {topic} into a daily habit",
    "Behind the scenes: {topic} in 60 seconds",
    "What {topic} actually looks like in 2026",
    "The one {topic} tip that changed everything",
    "{topic} for beginners: start here",
    "Why most {topic} advice is wrong",
    "POV: you just discovered {topic}",
    "3 tools I use for {topic} every day",
    "The dark side of {topic}",
    "Can you master {topic} in 7 days?",
)

IDEA_HOOKS: tuple[str, ...] = (
    "listicle",
    "mistake",
    "story",
    "behind-the-scenes",
    "trend",
    "tip",
    "tutorial",
    "hot-take",
    "pov",
    "tools",
    "controversy",
    "challenge",
)


def generate_ideas_deterministic(topic: str, count: int = 5) -> list[dict[str, str]]:
    """Generate post ideas without an LLM (deterministic template fallback).

    Args:
        topic: Content topic to build ideas around.
        count: Number of ideas to generate (clamped to 1-10).

    Returns:
        List of ``count`` dicts, each with "idea", "hook", and "source" keys.
    """
    topic = (topic or "").strip() or "content creation"
    count = max(1, min(int(count), 10))
    return [
        {
            "idea": IDEA_TEMPLATES[i % len(IDEA_TEMPLATES)].format(topic=topic),
            "hook": IDEA_HOOKS[i % len(IDEA_HOOKS)],
            "source": "deterministic",
        }
        for i in range(count)
    ]


async def generate_ideas_llm(
    topic: str,
    count: int = 5,
    llm_config: Any = None,
) -> list[dict[str, str]]:
    """Generate post ideas using an LLM.

    Requires a configured LLM endpoint. Falls back to deterministic
    generation when the LLM is not available.

    Returns:
        List of dicts with "idea", "hook", and "source" keys.
    """
    try:
        from xpst.knowledge.llm.client import LLMClient

        if llm_config is None:
            from xpst.knowledge.config import KnowledgeConfig
            llm_config = KnowledgeConfig.from_env()

        if not llm_config.llm_enabled or not llm_config.llm_base_url:
            logger.debug("LLM not configured, falling back to deterministic ideas")
            return generate_ideas_deterministic(topic, count)

        client = LLMClient(
            base_url=llm_config.llm_base_url,
            model=llm_config.llm_model or "gpt-4o-mini",
        )

        prompt = (
            f"Generate {count} creative social media post ideas about '{topic}'. "
            f"Each idea needs a short hook. "
            f"Return {count} ideas as JSON: "
            f'[{{"idea": "...", "hook": "..."}}]'
        )

        import asyncio

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.chat_json([{"role": "user", "content": prompt}]),
        )
        if isinstance(result, list):
            ideas = result
        elif isinstance(result, dict) and "ideas" in result:
            ideas = result["ideas"]
        else:
            ideas = [result]

        # Normalise + tag the source; trim to the requested count.
        normalized: list[dict[str, str]] = []
        for item in ideas[:count]:
            if isinstance(item, dict):
                normalized.append({
                    "idea": str(item.get("idea", "")).strip(),
                    "hook": str(item.get("hook", "")).strip(),
                    "source": "llm",
                })
        if not normalized or not normalized[0]["idea"]:
            raise ValueError("LLM returned no usable ideas")
        return normalized

    except Exception as e:
        logger.debug(f"LLM idea generation failed, using deterministic: {e}")
        return generate_ideas_deterministic(topic, count)


def generate_ideas(
    topic: str,
    count: int = 5,
    llm_config: Any = None,
) -> list[dict[str, str]]:
    """Generate post ideas (synchronous wrapper).

    Uses the LLM when configured (XPST_KB_LLM_ENABLED), otherwise falls back
    to deterministic template generation.

    Args:
        topic: Content topic to build ideas around.
        count: Number of ideas to generate (clamped to 1-10).
        llm_config: Optional KnowledgeConfig for LLM settings.

    Returns:
        List of dicts with "idea", "hook", and "source" keys.
    """
    import asyncio

    try:
        return asyncio.run(generate_ideas_llm(topic, count, llm_config))
    except RuntimeError:
        # Already in an event loop
        return generate_ideas_deterministic(topic, count)
