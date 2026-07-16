"""Trigram (pg_trgm) GIN indexes for fast substring search on product name,
brand, and variant color/size. Postgres-only; a no-op on other backends
(e.g. SQLite in local dev)."""
from django.db import migrations

TRGM_INDEXES = [
    ("product_name_trgm", "inventory_product", "name"),
    ("product_brand_trgm", "inventory_product", "brand"),
    ("variant_color_trgm", "inventory_productvariant", "color"),
    ("variant_size_trgm", "inventory_productvariant", "size"),
]

def create_trgm(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, col in TRGM_INDEXES:
        schema_editor.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin ({col} gin_trgm_ops)")

def drop_trgm(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for name, _t, _c in TRGM_INDEXES:
        schema_editor.execute(f"DROP INDEX IF EXISTS {name}")

class Migration(migrations.Migration):
    dependencies = [("inventory", "0028_branchstock_bs_cost_nonneg_and_more")]
    operations = [migrations.RunPython(create_trgm, drop_trgm)]
