from django.db import migrations


class Migration(migrations.Migration):
    """Payments now go through our own pay page with Razorpay orders, not Razorpay payment links."""

    dependencies = [
        ("billing", "0002_default_products"),
    ]

    operations = [
        migrations.RenameField(model_name="payment", old_name="razorpay_link_id", new_name="razorpay_order_id"),
        migrations.RemoveField(model_name="payment", name="link_url"),
    ]
