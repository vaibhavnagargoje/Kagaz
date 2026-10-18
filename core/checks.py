from django.conf import settings
from django.core.checks import Warning, register


@register(deploy=True)
def gemini_free_tier_warning(app_configs, **kwargs):
    """Shown by `manage.py check --deploy`: Gemini's free tier isn't for real personal documents."""
    if settings.AI_PROVIDER != "gemini":
        return []
    return [
        Warning(
            "On Gemini's free tier, Google may use submitted documents to improve its products "
            "and human reviewers may read them. That breaks our privacy promise.",
            hint="Enable billing on the Google Cloud project behind GEMINI_API_KEY, or set AI_PROVIDER=claude.",
            id="kagaz.W001",
        )
    ]
