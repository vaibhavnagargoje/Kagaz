from django.contrib import admin

from vault.models import Conversion, Document


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    """Only non-identifying columns. Files and details stay encrypted."""

    list_display = ("id", "member", "category", "side", "status", "is_sensitive", "mime_type", "size_bytes", "created_at")
    list_filter = ("status", "category", "is_sensitive", "mime_type")
    fields = (
        "member", "category", "side", "status", "is_sensitive", "mime_type", "size_bytes", "page_count",
        "expiry_date", "read_attempts", "next_read_at", "created_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Conversion)
class ConversionAdmin(admin.ModelAdmin):
    list_display = ("member", "operations", "input_bytes", "output_bytes", "met_target", "created_at")
    list_filter = ("met_target",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
