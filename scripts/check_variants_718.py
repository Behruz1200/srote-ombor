import django
django.setup()
from inventory.models import Product, ProductVariant
p = Product.objects.filter(pk=718).first()
print('mahsulot:', p.pk, p.code, p.name, '| is_open_price:', p.is_open_price)
vs = list(ProductVariant.objects.filter(product=p).order_by('pk'))
print('turlar:', len(vs))
for v in vs:
    print('  pk=%-6d size=%-10r color=%-14r barcode=%s' % (v.pk, v.size, v.color, v.barcode))
dups = {}
for v in vs:
    dups.setdefault((v.size, v.color), []).append(v.pk)
print('takror juftliklar:', {k: n for k, n in dups.items() if len(n) > 1})
print()
print('ICH-0001 kodli mahsulotlar:')
for q in Product.objects.filter(code='ICH-0001'):
    print('  pk=%d name=%s variants=%d' % (q.pk, q.name, q.variants.count()))
