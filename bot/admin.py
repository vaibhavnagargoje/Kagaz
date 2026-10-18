from django.contrib import admin

from bot.models import InboundMessage


@admin.register(InboundMessage)
class InboundMessageAdmin(admin.ModelAdmin):
    """Delivery log for debugging. Message contents are not shown."""

    list_display = ("wa_message_id", "msg_type", "status", "created_at", "processed_at")
    list_filter = ("status", "msg_type")
    fields = ("wa_message_id", "wa_id", "msg_type", "status", "created_at", "processed_at")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
