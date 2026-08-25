from django.test import TestCase

# Create your tests here.


class PriceTagRegressionTest(TestCase):
    """Raqamli KODlar (masalan '1273') narx deb qabul qilinmasligini qulflaydi.
    Aks holda turli tovarlar bir 'oila'ga qo'shilib, qabulда qoldiq noto'g'ri
    turga tushib, kassada 'omborda yo'q' chiqardi."""

    def test_numeric_codes_are_not_price_tags(self):
        from inventory.views import _base_color
        # Raqamli kodlar -> o'z rangi bo'lib qoladi
        for code in ('1273', '3033', '995', '3027', '01', '545'):
            self.assertEqual(_base_color(code), code,
                             f"{code} kod, narx emas — o'zgarmasligi kerak")
        # Haqiqiy (probelli) narxlar -> bo'sh base
        for price in ('115 000', '75 000', '1 500 000'):
            self.assertEqual(_base_color(price), '')
        # Rang + narx
        self.assertEqual(_base_color('Qora · 115 000'), 'Qora')
        self.assertEqual(_base_color('Oq'), 'Oq')
