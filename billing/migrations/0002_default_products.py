from django.db import migrations

PRODUCTS = [
    {"code": "plus_year", "name": "Kagaz Plus · 1 year", "button_label": "Yearly", "price": 399, "plus_days": 365, "order": 1},
    {"code": "plus_month", "name": "Kagaz Plus · 1 month", "button_label": "Monthly", "price": 49, "plus_days": 30, "order": 2},
    {"code": "conversions_10", "name": "10 conversions", "button_label": "10 conversions", "price": 19, "conversions": 10, "order": 3},
]


def add_defaults(apps, schema_editor):
    Product = apps.get_model("billing", "Product")
    ReferralProgram = apps.get_model("billing", "ReferralProgram")
    for product in PRODUCTS:
        Product.objects.get_or_create(code=product["code"], defaults=product)
    ReferralProgram.objects.get_or_create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(add_defaults, migrations.RunPython.noop),
    ]
