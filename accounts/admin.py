from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from accounts.models import Member, User

admin.site.register(User, UserAdmin)


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    """Support view of members. Keys and PIN hashes are never shown."""

    list_display = ("__str__", "display_name", "plan", "has_consented", "document_count", "last_active_at", "created_at")
    list_filter = ("plan", "is_blocked")
    search_fields = ("wa_id",)
    fields = (
        "wa_id", "display_name", "plan", "is_blocked", "consent_given_at", "last_active_at",
        "pin_reset_ready_at", "created_at",
    )
    readonly_fields = ("wa_id", "consent_given_at", "last_active_at", "pin_reset_ready_at", "created_at")

    @admin.display(boolean=True)
    def has_consented(self, member):
        return member.has_consented

    @admin.display(description="Documents")
    def document_count(self, member):
        return member.documents.count()

    def has_add_permission(self, request):
        return False
