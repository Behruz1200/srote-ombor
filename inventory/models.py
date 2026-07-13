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
    monthly_rent = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text="Oylik ijara (so'm). Filial P&L hisobida qatnashadi."
    )
    monthly_other_costs = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text="Oylik boshqa qat'iy xarajatlar: ish haqi, kommunal, internet va h.k."
    )
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
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Sotuvchi komissiyasi (sotuv summasidan foiz). 0 = komissiyasiz."
    )

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
    external_barcode = models.CharField(
        max_length=64, blank=True, null=True, unique=True, db_index=True,
        help_text="Ishlab chiqaruvchi tomonidan chop etilgan barcode "
                  "(EAN-13, UPC va h.k.). Skanerlash uchun ishlatiladi."
    )
    name = models.CharField(max_length=200)
    brand = models.CharField(
        max_length=100, blank=True, db_index=True,
        help_text="Brend nomi (masalan: Zara, Nike). Kiyim/poyabzal uchun ishlatiladi."
    )
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
        from django.db.models import Sum
        return BranchStock.objects.filter(variant__product=self).aggregate(
            s=Sum('stock_count')
        )['s'] or 0

    def total_value(self):
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        return BranchStock.objects.filter(variant__product=self).aggregate(
            v=Sum(ExpressionWrapper(
                F('stock_count') * F('cost_price'),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            )),
        )['v'] or 0


class ProductVariant(models.Model):
    """Mahsulotning o'lcham+rang varianti (filialdan mustaqil)"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    size = models.CharField(max_length=30)
    color = models.CharField(max_length=50)
    barcode = models.CharField(
        max_length=64, blank=True, null=True, unique=True, db_index=True,
        help_text="Turga xos shtrix-kod (EAN-13 va h.k.) — har tur uchun alohida."
    )

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
                                     help_text="Chakana narx (1 dona, so'm)")
    wholesale_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Ulgurji narx (1 dona, so'm). 0 bo'lsa chakana narx qo'llaniladi."
    )

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


class Supplier(models.Model):
    """Yetkazib beruvchi — qabul tarixini bog'lash uchun."""
    name = models.CharField(max_length=200, unique=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=255, blank=True)
    inn = models.CharField(max_length=14, blank=True,
                           help_text="STIR (yuridik shaxs uchun)")
    contact_person = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Yetkazib beruvchi'
        verbose_name_plural = 'Yetkazib beruvchilar'
        ordering = ['name']

    def __str__(self):
        return self.name


class IntakeSession(models.Model):
    """Bitta yetkazib (delivery) — ko'p mahsulot bir kelishda qabul qilinadi.
    Har Intake shu sessiyaga ulanadi."""
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT,
                               related_name='intake_sessions')
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL,
                                 null=True, blank=True,
                                 related_name='intake_sessions')
    supplier_text = models.CharField(
        max_length=200, blank=True,
        help_text="Supplier model'da yo'q bo'lsa, erkin matn"
    )
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.PROTECT,
                                    related_name='intake_sessions')
    invoice_number = models.CharField(max_length=80, blank=True,
                                      help_text="Yetkazib beruvchining faktura raqami")
    invoice_image = models.ImageField(upload_to='invoices/', blank=True, null=True,
                                      help_text="Faktura/dostavka fotografiyasi")
    note = models.TextField(blank=True)
    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Qabul sessiyasi'
        verbose_name_plural = 'Qabul sessiyalari'
        ordering = ['-received_at']

    def __str__(self):
        supplier = self.supplier.name if self.supplier else (self.supplier_text or '—')
        return f'#{self.pk} {self.branch.name} ← {supplier} ({self.received_at:%d.%m %H:%M})'

    @property
    def total_qty(self):
        return sum(i.quantity for i in self.intakes.all())

    @property
    def total_cost(self):
        return sum(i.total_cost for i in self.intakes.all())

    @property
    def variants_count(self):
        return self.intakes.count()

    @property
    def supplier_display(self):
        if self.supplier:
            return self.supplier.name
        return self.supplier_text or "—"


class Intake(models.Model):
    """Mahsulot kelib tushishi (qabul) — filialga"""
    session = models.ForeignKey(
        IntakeSession, on_delete=models.CASCADE,
        related_name='intakes', null=True, blank=True,
        help_text="Bir partiyadagi bir nechta qabullarni guruhlash"
    )
    supplier_ref = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='intakes',
        help_text="Yetkazib beruvchi (model). Eski yozuvlarda supplier matn'da."
    )
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='intakes')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='intakes')
    quantity = models.IntegerField(
        help_text="Manfiy bo'lishi mumkin — yetkazib beruvchiga qaytarish"
    )
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=2)
    supplier = models.CharField(max_length=200, blank=True)
    is_return = models.BooleanField(
        default=False,
        help_text="Yetkazib beruvchiga qaytarish (quantity manfiy)"
    )
    return_reason = models.CharField(max_length=200, blank=True)
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


class Customer(models.Model):
    """Mijoz — telefon raqami orqali yagona deb hisoblanadi."""
    name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, db_index=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    tags = models.CharField(
        max_length=200, blank=True,
        help_text="Vergul bilan: VIP, doimiy, optom..."
    )
    inn = models.CharField(
        max_length=14, blank=True,
        help_text="B2B mijoz uchun STIR (e-faktura)",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Mijoz'
        verbose_name_plural = 'Mijozlar'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['phone'])]

    def __str__(self):
        bits = [self.name or '—']
        if self.phone:
            bits.append(self.phone)
        return ' · '.join(bits)

    @property
    def display_name(self):
        return self.name or self.phone or f'#{self.pk}'


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
    shift = models.ForeignKey(
        'Shift', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions',
        help_text='Sotuv qaysi smen davomida amalga oshirilgan'
    )
    payment_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH,
        help_text="Asosiy yoki yagona to'lov turi. Mixed bo'lsa payment_breakdown'da batafsil."
    )
    payment_breakdown = models.JSONField(
        default=list, blank=True,
        help_text="Mixed to'lov bo'lganda har turning summasi: [{method, amount}]"
    )
    customer = models.ForeignKey(
        'Customer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions',
    )
    customer_name = models.CharField(max_length=120, blank=True)
    customer_phone = models.CharField(max_length=40, blank=True)
    customer_inn = models.CharField(
        max_length=14, blank=True,
        help_text="B2B sotuvlar uchun mijozning STIRi (e-faktura beriladi)"
    )
    note = models.CharField(max_length=200, blank=True)
    # ----- Discount (whole-order) -----
    order_discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Butun chek bo'yicha chegirma (so'm)"
    )
    discount_reason = models.CharField(
        max_length=200, blank=True,
        help_text="Chegirma sababi (audit uchun)"
    )
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
        indexes = [
            models.Index(fields=['sold_at'], name='saletxn_soldat_idx'),
            models.Index(fields=['branch', '-sold_at'], name='saletxn_branch_soldat_idx'),
            models.Index(fields=['sold_by', '-sold_at'], name='saletxn_seller_soldat_idx'),
            models.Index(fields=['payment_method', '-sold_at'], name='saletxn_pay_soldat_idx'),
        ]

    def __str__(self):
        return f'#{self.pk} — {self.branch.name} ({self.sold_at:%d.%m.%Y %H:%M})'

    @property
    def gross(self):
        """Hech qanday chegirmasiz."""
        return sum(s.gross for s in self.lines.all())

    @property
    def line_discount_total(self):
        return sum(s.line_discount for s in self.lines.all())

    @property
    def discount_total(self):
        """Qator + chek bo'yicha jami chegirma."""
        return self.line_discount_total + self.order_discount

    @property
    def total(self):
        """Mijoz to'lagan yakuniy summa."""
        return self.gross - self.discount_total

    @property
    def profit(self):
        # Line-level profit already accounts for line_discount via Sale.profit.
        # Subtract whole-order discount on top.
        return sum(s.profit for s in self.lines.all()) - self.order_discount

    @property
    def item_count(self):
        return sum(s.quantity for s in self.lines.all())


class Stocktake(models.Model):
    """Inventarizatsiya sessiyasi — filialdagi tovarni jismonan sanab chiqish."""

    class Status(models.TextChoices):
        OPEN = 'open', 'Sanalmoqda'
        APPLIED = 'applied', 'Tasdiqlangan'
        CANCELLED = 'cancelled', 'Bekor qilingan'

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='stocktakes')
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name='stocktakes_started')
    started_at = models.DateTimeField(default=timezone.now)
    applied_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='stocktakes_applied')
    applied_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = 'Inventarizatsiya'
        verbose_name_plural = 'Inventarizatsiyalar'
        ordering = ['-started_at']

    def __str__(self):
        return f'Inv #{self.pk} — {self.branch.name} ({self.started_at:%d.%m %H:%M})'

    def total_diff_qty(self):
        return sum((c.counted_qty - c.system_qty) for c in self.counts.all())


class StocktakeCount(models.Model):
    """Bitta variant uchun sanab chiqilgan miqdor."""
    session = models.ForeignKey(Stocktake, on_delete=models.CASCADE, related_name='counts')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    system_qty = models.IntegerField(
        help_text='Inventarizatsiya boshlangandagi tizim miqdori (snapshot)'
    )
    counted_qty = models.IntegerField(
        help_text='Jismonan sanab chiqilgan miqdor'
    )

    class Meta:
        unique_together = ('session', 'variant')
        ordering = ['variant__product__code', 'variant__size']

    @property
    def diff(self):
        return self.counted_qty - self.system_qty


class Transfer(models.Model):
    """Filiallar orasi tovar ko'chirish."""
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Tayyorlanmoqda'
        IN_TRANSIT = 'in_transit', 'Yo\'lda'
        RECEIVED = 'received', 'Qabul qilindi'
        CANCELLED = 'cancelled', 'Bekor qilindi'

    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT,
                                    related_name='transfers_out')
    to_branch = models.ForeignKey(Branch, on_delete=models.PROTECT,
                                  related_name='transfers_in')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name='transfers_created')
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='transfers_received')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = 'Tovar ko\'chirish'
        verbose_name_plural = "Tovar ko'chirishlar"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'dispatched_at'], name='transfer_status_disp_idx'),
            models.Index(fields=['from_branch', 'status'], name='transfer_from_status_idx'),
            models.Index(fields=['to_branch', 'status'], name='transfer_to_status_idx'),
        ]

    def __str__(self):
        return f'#{self.pk}: {self.from_branch.name} → {self.to_branch.name}'

    @property
    def total_qty(self):
        return sum(l.quantity for l in self.lines.all())


class TransferLine(models.Model):
    transfer = models.ForeignKey(Transfer, on_delete=models.CASCADE, related_name='lines')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    class Meta:
        verbose_name = 'Ko\'chirish qatori'
        verbose_name_plural = "Ko'chirish qatorlari"

    def __str__(self):
        return f'{self.variant} × {self.quantity}'


class Shift(models.Model):
    """Sotuvchining ish smen'i — kassa pulini hisoblash uchun."""

    class Status(models.TextChoices):
        OPEN = 'open', 'Ochiq'
        CLOSED = 'closed', 'Yopilgan'

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='shifts')
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='shifts_opened'
    )
    opened_at = models.DateTimeField(default=timezone.now)
    opening_cash = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Smen boshida kassada bo'lgan pul (so'm)"
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='shifts_closed'
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    counted_cash = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Smen oxirida sanab chiqilgan naqd"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = 'Smen'
        verbose_name_plural = 'Smenlar'
        ordering = ['-opened_at']
        constraints = [
            # Bitta filialda bir vaqtda faqat bitta ochiq smen bo'lishi mumkin
            models.UniqueConstraint(
                fields=['branch'], condition=models.Q(status='open'),
                name='one_open_shift_per_branch'
            ),
        ]
        indexes = [
            models.Index(fields=['branch', '-opened_at'], name='shift_branch_opened_idx'),
            models.Index(fields=['status', '-opened_at'], name='shift_status_opened_idx'),
        ]

    def __str__(self):
        return f'Smen #{self.pk} — {self.branch.name} ({self.opened_at:%d.%m %H:%M})'

    def cash_sales(self):
        """Smen davomida naqd to'lov qilingan sotuvlar summasi."""
        end = self.closed_at or timezone.now()
        rev_expr = models.ExpressionWrapper(
            models.F('quantity') * models.F('sale_price') - models.F('line_discount'),
            output_field=models.DecimalField(max_digits=14, decimal_places=2)
        )
        cash_txn_ids = SaleTransaction.objects.filter(
            branch=self.branch, sold_at__gte=self.opened_at, sold_at__lt=end,
            payment_method='cash',
        ).values_list('id', flat=True)
        line_rev = Sale.objects.filter(
            transaction_id__in=cash_txn_ids
        ).aggregate(s=models.Sum(rev_expr))['s'] or 0
        order_disc = SaleTransaction.objects.filter(
            id__in=cash_txn_ids
        ).aggregate(s=models.Sum('order_discount'))['s'] or 0
        return line_rev - order_disc

    def expected_cash(self):
        """Kutilgan naqd = ochilish + smen davomidagi naqd sotuvlar."""
        return self.opening_cash + self.cash_sales()

    def variance(self):
        """Kassa farqi = sanalgan − kutilgan. Manfiy bo'lsa kam, ortiq bo'lsa ko'p."""
        if self.counted_cash is None:
            return None
        return self.counted_cash - self.expected_cash()


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
    line_discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Bu qatorga berilgan chegirma (so'm)"
    )
    sold_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name='sales')
    sold_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = 'Sotuv'
        verbose_name_plural = 'Sotuvlar'
        ordering = ['-sold_at']
        indexes = [
            models.Index(fields=['sold_at'], name='sale_soldat_idx'),
            models.Index(fields=['branch', '-sold_at'], name='sale_branch_soldat_idx'),
            models.Index(fields=['variant', '-sold_at'], name='sale_variant_soldat_idx'),
            models.Index(fields=['sold_by', '-sold_at'], name='sale_seller_soldat_idx'),
            models.Index(fields=['transaction', 'sold_at'], name='sale_txn_soldat_idx'),
        ]

    @property
    def gross(self):
        """Chegirmasiz summa."""
        return self.quantity * self.sale_price

    @property
    def total(self):
        """Chegirmadan keyingi summa."""
        return self.gross - self.line_discount

    @property
    def profit(self):
        return self.total - (self.quantity * self.cost_at_sale)

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


class PaymentQR(models.Model):
    """Static QR — har filial uchun provider'ning oldindan chop etilgan kodi.
    Mijoz scan qilib o'z ilovasidan summa kiritib to'laydi. Kassir
    manual ravishda 'to'lov olindi' deb tasdiqlaydi."""

    class Provider(models.TextChoices):
        PAYME = 'payme', 'Payme'
        CLICK = 'click', 'Click'
        UZUM = 'uzum', 'Uzum Bank'
        HUMO = 'humo', 'Humo Pay'
        ANOR = 'anor', 'Anor (muddatli)'
        ALIF = 'alif', 'Alif (muddatli)'
        IMAN = 'iman', 'Iman (muddatli)'
        ZOODPAY = 'zoodpay', 'Zoodpay (muddatli)'
        OTHER = 'other', 'Boshqa'

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE,
                               related_name='payment_qrs')
    provider = models.CharField(max_length=20, choices=Provider.choices)
    label = models.CharField(
        max_length=120, blank=True,
        help_text="Ko'rsatma: \"Yetakchi hisob\", \"Qo'shimcha karta\" va h.k."
    )
    qr_image = models.ImageField(
        upload_to='payment_qrs/', blank=True, null=True,
        help_text="QR rasm fayli. Bo'sh bo'lsa qr_payload'dan generatsiya qilinadi."
    )
    qr_payload = models.CharField(
        max_length=500, blank=True,
        help_text="QR ichidagi matn/URL (masalan to'lov ilovasiga deeplink). "
                  "qr_image bo'lmasa shu asosida QR generatsiya qilinadi."
    )
    instructions = models.CharField(
        max_length=200, blank=True,
        help_text="Mijozga ko'rsatma: \"Summa: TOTAL so'm\" yoki maxsus izoh"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'To\'lov QR'
        verbose_name_plural = "To'lov QR'lar"
        ordering = ['branch', 'provider']

    def __str__(self):
        return f'{self.branch.name} · {self.get_provider_display()}'


class PaymentIntent(models.Model):
    """Kassir QR tugmasini bosganda yaratiladigan to'lov niyati.
    Mijoz to'lagandan keyin provider webhook/polling orqali status 'paid'
    bo'ladi va POS avtomatik chekni yakunlaydi."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Kutilmoqda'
        PAID = 'paid', "To'langan"
        CANCELLED = 'cancelled', 'Bekor qilindi'
        EXPIRED = 'expired', 'Muddati tugadi'

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE,
                               related_name='payment_intents')
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                     on_delete=models.PROTECT,
                                     related_name='payment_intents_initiated')
    provider = models.CharField(max_length=20, help_text="payme, click, uzum...")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    ref_code = models.CharField(
        max_length=12, db_index=True,
        help_text="Mijoz to'lov izohi sifatida kiritadigan kod"
    )
    cart_snapshot = models.TextField(
        blank=True,
        help_text="POS savatining JSON snapshot'i — paid bo'lgach checkout uchun"
    )
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    provider_txn_id = models.CharField(max_length=120, blank=True,
                                       help_text="Provider tomonidan berilgan ID")
    created_at = models.DateTimeField(default=timezone.now)
    paid_at = models.DateTimeField(null=True, blank=True)
    sale_transaction = models.OneToOneField(
        'SaleTransaction', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payment_intent',
        help_text="Paid bo'lib checkout amalga oshgan bo'lsa, shu SaleTransaction"
    )

    class Meta:
        verbose_name = "To'lov niyati"
        verbose_name_plural = "To'lov niyatlari"
        ordering = ['-created_at']

    def __str__(self):
        return f'#{self.pk} {self.provider} {self.amount} ({self.status})'


class Promotion(models.Model):
    """Aksiya/kampaniya — POS chekida avtomatik qo'llaniladi.

    Tur:
      - percent_off: butun chekka yoki kategoriya/mahsulotga foiz chegirma
        (min_qty mahsulot kerak)
      - buy_x_get_y: N ta olganda M ta arzonroq mahsulot bepul
        (qty_required = N, qty_free = M, target category/products)
      - nth_percent_off: har qty_required-mahsulotga foiz chegirma
    """
    class Type(models.TextChoices):
        PERCENT_OFF = 'percent_off', 'Foiz chegirma'
        BUY_X_GET_Y = 'buy_x_get_y', "N olganga M bepul"
        NTH_PERCENT = 'nth_percent_off', 'Har N-mahsulotga chegirma'

    name = models.CharField(max_length=120)
    promo_type = models.CharField(max_length=30, choices=Type.choices,
                                  default=Type.PERCENT_OFF)
    percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Foiz chegirma (0-100). percent_off va nth_percent_off uchun."
    )
    qty_required = models.PositiveIntegerField(
        default=1,
        help_text="Buy-X-Get-Y'da X. Nth_percent'da N. percent_off'da min mahsulot soni."
    )
    qty_free = models.PositiveIntegerField(
        default=0,
        help_text="Buy-X-Get-Y'da Y (eng arzonidan)"
    )
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='promotions',
        help_text="Faqat shu kategoriyaga. Bo'sh — barcha mahsulotlar."
    )
    target_products = models.ManyToManyField(
        Product, blank=True, related_name='promotions',
        help_text="Aniq mahsulotlar. Bo'sh va kategoriya bo'sh — barchasi."
    )
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True,
                                       help_text="Bo'sh — muddatsiz")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Aksiya'
        verbose_name_plural = 'Aksiyalar'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.get_promo_type_display()})'

    def is_live(self):
        now = timezone.now()
        if not self.is_active or self.valid_from > now:
            return False
        if self.valid_until and self.valid_until < now:
            return False
        return True


class ParkedSale(models.Model):
    """Vaqtincha saqlangan (park qilingan) savat — kassir keyinroq davom ettiradi."""
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='parked_sales')
    parked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name='parked_sales')
    label = models.CharField(max_length=80, help_text="Mijoz ismi yoki belgi")
    cart_json = models.TextField(help_text="Savat snapshoti (JSON)")
    customer_name = models.CharField(max_length=120, blank=True)
    customer_phone = models.CharField(max_length=30, blank=True)
    order_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Park qilingan savat'
        verbose_name_plural = 'Park qilingan savatlar'
        ordering = ['-created_at']

    def __str__(self):
        return f'#{self.pk} {self.label} ({self.branch.name})'


class Return(models.Model):
    """Qaytarilgan mahsulot — sotuv qatorini qisman yoki to'liq qaytarish."""
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name='returns')
    shift = models.ForeignKey(
        'Shift', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='returns',
        help_text="Refund qaysi smen davomida amalga oshgan — kassa farqiga ta'sir"
    )
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
        # Use line total (after line_discount) so refunds match actual cash returned
        if self.sale.quantity > 0:
            per_unit_total = self.sale.total / self.sale.quantity
            return self.quantity * per_unit_total
        return self.quantity * self.sale.sale_price
