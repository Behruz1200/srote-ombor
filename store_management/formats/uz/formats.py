# O'zbek TILI (xabarlar uzbekcha) — LEKIN raqamlar NUQTA-o'nlik bo'lib qolsin.
# Django L10N yoqilганда uz lokали vergul-o'nlik ("133000,00") beradi; bu
# JS count-up parserни va type="number" inputlarни buzardi (100× xato).
# FORMAT_MODULE_PATH shu faylни eng yuqori ustunlik bilan ishlatadi.
DECIMAL_SEPARATOR = '.'
THOUSAND_SEPARATOR = ' '
USE_THOUSAND_SEPARATOR = False
NUMBER_GROUPING = 3
