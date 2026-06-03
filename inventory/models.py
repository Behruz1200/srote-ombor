from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.utils import timezone


class Branch(models.Model):
    name = models.CharField(max_length=120, unique=True)
    address = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    inn = models.CharField(
        max_length=14, blank=True,
        help_text="STIR / INN (Soliq fiskalizatsiyasi uchun). "
                  "Yuridik shaxs uchun 9 raqam, jismoniy shaxs uchun 14."
    )
    fiscal_module_id = models.CharField(
        max_length=80, blank=True,
        help_text="Filialning kassa apparati ID (OFD beradi)"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Filial'
        verbose_name_plural = 'Filiallar'
        ordering = ['name']

    def __str__(self):
        return self.name


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Administrator'
        SOTUVCHI = 'sotuvchi', 'Sotuvchi'

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.SOTUVCHI)
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='staff',
                               help_text="Sotuvchi ishlaydigan filial")

    def is_admin(self):
        return self.role == self.Role.ADMIN or self.is_superuser


class Category(models.Model):
    name = models.CharField(max_length=120, unique=True)
    prefix = models.CharField(
        max_length=6, unique=True, blank=True,
        help_text="Mahsulot kodi prefiksi (masalan: OYO, KOY). Bo'sh qoldirilsa, "
                  "nom asosida avtomatik tanlanadi."
    )

    class Meta:
        verbose_name = 'Kategoriya'
        verbose_name_plural = 'Kategoriyalar'
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.prefix:
            self.prefix = self._derive_prefix()
        else:
            self.prefix = self.prefix.upper().strip()
        super().save(*args, **kwargs)

    def _derive_prefix(self):
        import re
        chars = re.findall(r'[A-Za-z0-9]', self.name.replace("'", ""))
        base = ''.join(chars[:3]).upper() or 'CAT'
        candidate = base
        n = 1
        while Category.objects.filter(prefix=candidate).exclude(pk=self.pk).exists():
            n += 1
            candidate = (base[:5] + str(n))[:6]
        return candidate


class Product(models.Model):
    code = models.CharField(max_length=20, unique=True, db_index=True,
                            blank=True, editable=False)
    name = models.CharField(max_length=200)
    category = models.ForeignKey(Category, on_delete=models.PROTECT,
                                 related_name='products', null=True, blank=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    default_sale_price = models.DecimalField(max_digits=12, decimal_places=2,
                                             default=0, help_text="Sotuv narxi (so'm)")
    markup_percent = models.DecimalField(
        max_digits=6, decimal_places=2, default=40,
        help_text="Foiz: sotuv narxi = tannarx × (1 + foiz/100)"
    )
    # ----- Soliq / fiscal fields (used when OFD provider is connected) -----
    mxik_code = models.CharField(
        max_length=20, blank=True,
        help_text="Mahsulot xizmat international klassifikator kodi (soliq.uz)"
    )
    unit_code = models.CharField(
        max_length=20, blank=True, default='796',  # 796 = "штука / dona"
        help_text="Birlik kodi (OKEI). 796 = dona, 166 = kg, 778 = juft."
    )
    vat_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=12,
        help_text="QQS foizi. UZ standart: 12%, ba'zi tovarlar uchun 0% yoki 15%."
    )
    package_code = models.CharField(
        max_length=20, blank=True,
        help_text="Markirovka kodi (zarur tovarlar uchun; aks holda bo'sh)"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Mahsulot'
        verbose_name_plural = 'Mahsulotlar'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.code} — {self.name}'

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self._generate_code()
        super().save(*args, **kwargs)

    def _generate_code(self):
        if self.category and self.category.prefix:
            prefix = self.category.prefix
        else:
            prefix = 'PRD'
        max_n = 0
        for c in Product.objects.filter(code__startswith=f'{prefix}-').values_list('code', flat=True):
            try:
                n = int(c.split('-', 1)[1])
                if n > max_n:
                    max_n = n
            except (ValueError, IndexError):
                pass
        return f'{prefix}-{(max_n + 1):04d}'

    def total_stock(self):
        return sum(bs.stock_count for bs in BranchStock.objects.filter(variant__product=self))

    def total_value(self):
        return sum(
            bs.stock_count * bs.cost_price
            for bs in BranchStock.objects.filter(variant__product=self)
        )


class ProductVariant(models.Model):
    """Mahsulotning o'lcham+rang varianti (filialdan mustaqil)"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    size = models.CharField(max_length=30)
    color = models.CharField(max_length=50)

    class Meta:
        unique_together = ('product', 'size', 'color')
        ordering = ['size', 'color']

    def __str__(self):
        return f'{self.product.code} — {self.size} / {self.color}'


class BranchStock(models.Model):
    """Filialdagi mavjud zaxira (variant × filial)"""
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE,
                                related_name='branch_stocks')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE,
                               related_name='stocks')
    stock_count = models.PositiveIntegerField(default=0)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                     help_text="Tannarx (1 dona, so'm)")
    sale_price = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                     help_text="Sotuv narxi (1 dona, so'm)")

    @property
    def margin(self):
        """Foiz: sotuv − tannarx normalize qilingan."""
        if self.cost_price and self.cost_price > 0:
            return (self.sale_price - self.cost_price) / self.cost_price * 100
        return 0

    class Meta:
        unique_together = ('variant', 'branch')
        verbose_name = 'Zaxira'
        verbose_name_plural = 'Zaxiralar'
        ordering = ['branch', 'variant']

    def __str__(self):
        return f'{self.branch.name}: {self.variant} = {self.stock_count}'


class Intake(models.Model):
    """Mahsulot kelib tushishi (qabul) — filialga"""
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='intakes')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='intakes')
    quantity = models.PositiveIntegerField()
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=2)
    supplier = models.CharField(max_length=200, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name='intakes')
    received_at = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Qabul'
        verbose_name_plural = 'Qabullar'
        ordering = ['-received_at']

    @property
    def total_cost(self):
        return self.quantity * self.cost_per_unit


class AuditLog(models.Model):
    """Har bir muhim yozuvga (kim, qachon, nima qildi) izlash."""

    class Action(models.TextChoices):
        CREATE = 'create', 'Yaratdi'
        UPDATE = 'update', "O'zgartirdi"
        DELETE = 'delete', "O'chirdi"
        LOGIN = 'login', 'Kirdi'
        LOGOUT = 'logout', 'Chiqdi'
        LOGIN_FAILED = 'login_failed', "Kirish urinishi (xato)"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='audit_logs',
    )
    username_snapshot = models.CharField(
        max_length=150, blank=True,
        help_text='User o\'chirilsa ham foydalanuvchi nomi qoladi'
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    model_name = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=80, blank=True)
    object_repr = models.CharField(
        max_length=300, blank=True,
        help_text="Qaysi obyekt: masalan 'Sale: OYO-0001 — 3 dona'"
    )
    changes = models.JSONField(
        default=dict, blank=True,
        help_text='{"field": ["old", "new"], ...}'
    )
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = 'Audit log'
        verbose_name_plural = 'Audit log'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['model_name', 'object_id']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} — {self.username_snapshot} {self.action}'


class SaleTransaction(models.Model):
    """Sotuv chek'i: bir nechta mahsulot bir vaqtda mijozga sotilganda."""

    class PaymentMethod(models.TextChoices):
        CASH = 'cash', 'Naqd'
        CARD = 'card', 'Karta'
        TRANSFER = 'transfer', "O'tkazma"
        MIXED = 'mixed', 'Aralash'

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='transactions')
    sold_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name='transactions')
    sold_at = models.DateTimeField(default=timezone.now)
    payment_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    customer_name = models.CharField(max_length=120, blank=True)
    customer_phone = models.CharField(max_length=40, blank=True)
    customer_inn = models.CharField(
        max_length=14, blank=True,
        help_text="B2B sotuvlar uchun mijozning STIRi (e-faktura beriladi)"
    )
    note = models.CharField(max_length=200, blank=True)
    # ----- Soliq / fiscal -----
    fiscal_receipt_number = models.CharField(
        max_length=80, blank=True,
        help_text="OFD bergan fiskal chek raqami (provider qaytaradi)"
    )
    fiscal_qr_url = models.URLField(
        blank=True,
        help_text="OFD bergan QR kod URL'i — chekka chiqaramiz, mijoz tekshirishi uchun"
    )
    fiscal_status = models.CharField(
        max_length=20, blank=True,
        choices=[('pending', 'Kutilmoqda'), ('sent', 'Yuborilgan'),
                 ('failed', 'Xatolik'), ('skipped', "O'tkazib yuborildi")],
        help_text="OFD'ga yuborish holati"
    )
    fiscal_error = models.TextField(
        blank=True,
        help_text="Yuborish muvaffaqiyatsiz bo'lgan bo'lsa, xatolik matni"
    )

    class Meta:
        verbose_name = 'Sotuv chek'
        verbose_name_plural = 'Sotuv cheklari'
        ordering = ['-sold_at']

    def __str__(self):
        return f'#{self.pk} — {self.branch.name} ({self.sold_at:%d.%m.%Y %H:%M})'

    @property
    def total(self):
        return sum(s.total for s in self.lines.all())

    @property
    def profit(self):
        return sum(s.profit for s in self.lines.all())

    @property
    def item_count(self):
        return sum(s.quantity for s in self.lines.all())


class Sale(models.Model):
    """Bitta sotuv qatori (chek ichidagi mahsulot)."""
    transaction = models.ForeignKey(
        SaleTransaction, on_delete=models.CASCADE,
        related_name='lines', null=True, blank=True,
        help_text="Bitta chekka tegishli sotuvlar guruhi. Eski sotuvlar uchun bo'sh."
    )
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name='sales')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='sales')
    quantity = models.PositiveIntegerField()
    sale_price = models.DecimalField(max_digits=12, decimal_places=2)
    cost_at_sale = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Sotuv paytidagi tannarx (keyinroq narx o'zgarsa ham aniq foyda hisobi uchun)"
    )
    sold_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name='sales')
    sold_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = 'Sotuv'
        verbose_name_plural = 'Sotuvlar'
        ordering = ['-sold_at']

    @property
    def total(self):
        return self.quantity * self.sale_price

    @property
    def profit(self):
        return self.quantity * (self.sale_price - self.cost_at_sale)

    @property
    def margin(self):
        if self.sale_price and self.sale_price > 0:
            return (self.sale_price - self.cost_at_sale) / self.sale_price * 100
        return 0

    @property
    def returned_qty(self):
        # Use annotation if the queryset provided one (avoids N+1 on lists)
        if hasattr(self, '_returned'):
            return self._returned
        return self.returns.aggregate(s=models.Sum('quantity'))['s'] or 0


class Return(models.Model):
    """Qaytarilgan mahsulot — sotuv qatorini qisman yoki to'liq qaytarish."""
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name='returns')
    quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=200, blank=True,
                              help_text='Sabab: nuqson, kichik o\'lcham, mijoz fikri o\'zgardi...')
    refunded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name='returns')
    refunded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Qaytarilish'
        verbose_name_plural = 'Qaytarilishlar'
        ordering = ['-refunded_at']

    def __str__(self):
        return f'Qaytarilish: {self.sale.variant.product.code} × {self.quantity}'

    @property
    def refund_amount(self):
        return self.quantity * self.sale.sale_price
