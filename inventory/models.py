import uuid
from decimal import Decimal, InvalidOperation
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.utils import timezone


def _dec(x):
    """Coerce int/float/Decimal/str to Decimal for safe money arithmetic.

    Form-posted amounts arrive as float; DB values are Decimal. Mixing the
    two in a subtraction raises TypeError, so normalise before doing math.
    """
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _norm_pay_method(method):
    """To'lov turini {cash, card, transfer} ga normallashtiradi (MON-11/PAY-2).

    FAQAT aniq 'cash'/'card'/'transfer' o'zicha qoladi. QR-provayder nomlari
    (payme, click, uzum, humo, ...) va noma'lum/xato ('naqd' kabi) qiymatlar
    'transfer' bo'ladi — chunki ular kassadagi JISMONIY naqd EMAS. Noma'lum
    turni jimgina 'cash'ga qo'shib yuborish kassani "ortiq" ko'rsatardi.
    """
    m = (method or '').strip().lower()
    if m in ('cash', 'card', 'transfer'):
        return m
    return 'transfer'


def weighted_cost(old_qty, old_cost, add_qty, add_cost):
    """STK-8: o'rtacha-tortilgan (weighted-average) tannarx.

    Do'kon bir tovarni turli vaqtда har xil narxда oladi. Ilgari yangi qabul
    ESKI tannarxни butunlay almashtirar edi ('last cost') — javonда turgan eski
    zaxira ham yangi narxда hisoblanib, marja noto'g'ri chiqardi. Endi:
        yangi_tannarx = (eski_qoldiq×eski_tannarx + yangi×yangi) / jami
    Butun so'mga yaxlitlanadi (kopek yo'q — so'mда mayda birlik ishlatilmaydi).
    Eski qoldiq <= 0 bo'lsa — shunchaki yangi tannarx.
    """
    oq = max(0, int(old_qty or 0))
    aq = max(0, int(add_qty or 0))
    oc = _dec(old_cost or 0)
    ac = _dec(add_cost or 0)
    # STK-10: yangi tannarx 0 yoki noaniq bo'lsa (faktura o'qilmadi) — ESKI
    # tannarxни SAQLAYMIZ, 0 ga tushirmaymiz. Aks holda POS cost_at_sale=0
    # oladi va har sotuv 100% marja/nol tannarx bilan yozilardi.
    if ac <= 0:
        return oc
    denom = oq + aq
    # Yangi qabul yo'q (aq=0 — narx tuzatish) yoki eski qoldiq yo'q → yangi tannarx
    if denom <= 0 or oq <= 0 or aq <= 0:
        return ac
    total = oq * oc + aq * ac
    return (total / denom).quantize(Decimal('1'))  # butun so'm


def split_breakdown(net, breakdown):
    """Aralash (mixed) chekning SOF summasini {cash, card, transfer} ga bo'ladi.

    ARCH-6: bu YAGONA manba. Ilgari bu mantiq 4 joyда qayta yozilgan edi
    (models: sales_by_method_split, returns_by_method; views: dashboard
    _by_method, _returns_by_method) — ular asta-sekin bir-biridan farq qilib,
    §3.2 regressiyalarining ildizi bo'lgan. Endi hammasi shuni chaqiradi.

    Qoidalar:
      • KIRITILGAN summalar bo'yicha bo'linadi (nisbatga bo'lish EMAS).
      • Oxirgi usul qoldiqni oladi (yaxlitlash tufayli jami buzilmaydi).
      • Usullar _norm_pay_method orqali normallashtiriladi: 'payme'/'click'/
        noma'lum → 'transfer' (jimgina 'cash'ga aylanmaydi — MON-11).
      • Breakdown bo'sh/yaroqsiz bo'lsa — butun summa 'cash' (mavjud xatti-
        harakat saqlanadi; bunday cheklar checkoutда 'cash'ga aylantiriladi).
    net va amount har xil turdan (int/float/str/Decimal) kelishi mumkin —
    hammasi Decimalga o'giriladi.
    """
    net = _dec(net)
    out = {'cash': Decimal('0'), 'card': Decimal('0'), 'transfer': Decimal('0')}
    parts = {}
    s = Decimal('0')
    for e in (breakdown or []):
        try:
            mm = _norm_pay_method(e.get('method'))
            a = _dec(str(e.get('amount') or 0))
        except (AttributeError, TypeError, ValueError, InvalidOperation):
            continue
        if a <= 0:
            continue
        parts[mm] = parts.get(mm, Decimal('0')) + a
        s += a
    if s <= 0:
        out['cash'] += net
        return out
    keys = list(parts.keys())
    done = Decimal('0')
    for i, mm in enumerate(keys):
        if i == len(keys) - 1:
            share = net - done
        else:
            avail = net - done
            share = parts[mm] if parts[mm] <= avail else avail
            if share < Decimal('0'):
                share = Decimal('0')
        done += share
        out[mm] += share  # mm allaqachon cash/card/transfer
    return out


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
        validators=[MinValueValidator(0), MaxValueValidator(100)],  # MON-27
        help_text="Sotuvchi komissiyasi (sotuv summasidan foiz, 0-100). 0 = komissiyasiz."
    )
    # ----- Opt-in TOTP 2FA -----
    totp_secret = models.CharField(max_length=64, blank=True, editable=False)
    totp_confirmed = models.BooleanField(default=False)
    recovery_codes = models.JSONField(default=list, blank=True, editable=False)
    # AUTH-3: oxirgi qabul qilingan TOTP vaqt-qadami — bir kod ikki marta
    # ishlatilmasin (replay). Ushlangan kod 90 soniya amal qilardi.
    last_totp_step = models.BigIntegerField(null=True, blank=True, editable=False)

    def is_admin(self):
        return self.role == self.Role.ADMIN or self.is_superuser


class Group(models.Model):
    """Yuqori darajali bo'lim: Erkaklar, Ayollar, Bolalar, Parfumeriya/Uy.

    Kategoriyalar shu bo'limlarga tegishli bo'ladi; mahsulot bo'limni
    o'z kategoriyasidan oladi. 4 ta bo'lim boshlang'ich migratsiyada
    yaratiladi (men / women / kids / home).
    """
    slug = models.SlugField(max_length=20, unique=True)
    name = models.CharField(max_length=60)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "Bo'lim"
        verbose_name_plural = "Bo'limlar"
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=120, unique=True)
    group = models.ForeignKey(
        'Group', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='categories',
        help_text="Bo'lim: Erkaklar / Ayollar / Bolalar / Parfumeriya-Uy"
    )
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
    # Nakladnoy (AI qabul) bir xil shtrix-kodga BOSHQA nom bergan bo'lsa —
    # avtomatik qayta nomlamaymiz: taklif qilingan nomni shu yerga yozamiz
    # va Mahsulotlar sahifasida "2 xil nom" ogohlantirishi chiqadi. Foydalanuvchi
    # qaysi nomni qoldirishni tanlaydi (keyin bu maydon bo'shatiladi).
    pending_name = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Nakladnoydan kelgan muqobil nom (tasdiqlanmagan).")
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    default_sale_price = models.DecimalField(max_digits=12, decimal_places=2,
                                             default=0, help_text="Sotuv narxi (so'm)")
    markup_percent = models.DecimalField(
        max_digits=6, decimal_places=2, default=0,
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
    is_open_price = models.BooleanField(
        default=False,
        help_text="Ochiq narx (qo'lda summa) — kiyim/poyabzalni tizimga "
                  "kiritmasdan sotish uchun. Ombor tekshirilmaydi/kamaymaydi."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Mahsulot'
        verbose_name_plural = 'Mahsulotlar'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(default_sale_price__gte=0),
                                   name='product_sale_price_nonneg'),
            models.CheckConstraint(condition=models.Q(markup_percent__gte=0),
                                   name='product_markup_nonneg'),
        ]

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
        constraints = [
            models.CheckConstraint(condition=models.Q(cost_price__gte=0), name='bs_cost_nonneg'),
            models.CheckConstraint(condition=models.Q(sale_price__gte=0), name='bs_sale_nonneg'),
            models.CheckConstraint(condition=models.Q(wholesale_price__gte=0), name='bs_wholesale_nonneg'),
        ]

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
    agent_name = models.CharField(
        max_length=120, blank=True,
        help_text="Fakturani olib kelgan agent / ekspeditor")
    agent_phone = models.CharField(
        max_length=40, blank=True, help_text="Agent telefoni")
    invoice_image = models.ImageField(upload_to='invoices/', blank=True, null=True,
                                      help_text="Faktura/dostavka fotografiyasi")
    note = models.TextField(blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    # S4: foto-qabulни ikki marta qabul qilishни bloklaydi (LocMemCache workerlar
    # aro ishlamaydi — DB unikал kaliti ishonchli).
    idempotency_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True, editable=False)

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
    # STK-12: qabul (xarid) tarixi CASCADE bilan o'chib ketmasin. Ilgari
    # sotuvsiz mahsulot o'chirilса, uning variantlari → BranchStock → Intake
    # zanjiri jimgina yo'q bo'lardi (tovar javonда, tizimда yo'q). PROTECT
    # tarixli variantni o'chirishni bloklaydi (product_delete/variant_delete
    # allaqachon ProtectedError'ni ushlab, tushunarli xabar beradi). Merge
    # Intake'ni avval targetга ko'chirgani uchun buzilmaydi.
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name='intakes')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='intakes')
    quantity = models.IntegerField(
        help_text="Manfiy bo'lishi mumkin — yetkazib beruvchiga qaytarish"
    )
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=2)
    sale_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Shu qabuldagi sotuv narxi. Eski yozuvlarda bo'sh."
    )
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
        constraints = [
            models.CheckConstraint(condition=models.Q(cost_per_unit__gte=0), name='intake_cost_nonneg'),
        ]

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

    # SEC-6: URL'да ketma-ket ID o'rniga tasodifiy token — chekни tashqi havolada
    # ochganда umumiy savdo sonini oshkor qilmaydi va ID'larni "yurib" chiqib
    # bo'lmaydi. Ichki PK o'zgarmaydi.
    public_id = models.UUIDField(default=uuid.uuid4, editable=False,
                                 db_index=True, null=True)
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
    # OFF-2: offline savdo qayta yuborilganda (wifi miltillasa) bir sotuv IKKI
    # marta yozilmasin. Mijoz tomonidagi kalit — bir xil kalit ikkinchi chek
    # YARATMAYDI (unique). null bo'lsa cheklov yo'q (oddiy onlayn savdo).
    idempotency_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True,
        help_text="Offline replay uchun bir martalik kalit (takroriy chekni bloklaydi)"
    )
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
        constraints = [
            models.CheckConstraint(condition=models.Q(order_discount__gte=0), name='saletxn_orderdisc_nonneg'),
            # PAY-2: DB darajasida noto'g'ri to'lov turi (masalan 'payme') hech
            # qachon saqlanmasin — TextChoices o'zi DB constraint qo'ymaydi.
            models.CheckConstraint(
                condition=models.Q(payment_method__in=['cash', 'card', 'transfer', 'mixed']),
                name='saletxn_payment_method_valid'),
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
        return f'TransferLine #{self.pk}: {self.variant_id} × {self.quantity}'


class StockWriteOff(models.Model):
    """Tovarni hisobdan chiqarish (STK-1) — shikast, yo'qolish, buzilish,
    o'g'irlik, yetkazuvchiga qaytarish. MAJBURIY sabab kodi, faqat admin.

    Zaxira shu miqdorда kamayadi va bu yozuv ombor tenglamasiga kiradi. Aks
    holda haqiqiy yo'qotishlar 'nol narxli sotuv' yoki inventarizatsiya orqali
    yashirinib, o'g'irlik signalini yo'q qilardi.
    """
    class Reason(models.TextChoices):
        DAMAGE = 'damage', 'Shikastlangan'
        LOSS = 'loss', "Yo'qolgan"
        SPOILAGE = 'spoilage', "Buzilgan / muddati o'tgan"
        THEFT = 'theft', "O'g'irlangan"
        SUPPLIER_RETURN = 'supplier_return', 'Yetkazuvchiga qaytarildi'
        CORRECTION = 'correction', 'Tuzatish (inventarizatsiya)'
        OTHER = 'other', 'Boshqa'

    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT,
                                related_name='writeoffs')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT,
                               related_name='writeoffs')
    quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=20, choices=Reason.choices)
    note = models.CharField(max_length=200, blank=True)
    cost_at_writeoff = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Yozuv paytidagi tannarx (yo'qotish qiymatini hisoblash uchun)")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name='writeoffs')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = 'Hisobdan chiqarish'
        verbose_name_plural = 'Hisobdan chiqarishlar'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0),
                                   name='writeoff_qty_positive'),
        ]

    def __str__(self):
        return f'WriteOff #{self.pk}: {self.variant_id} × {self.quantity} ({self.reason})'

    @property
    def loss_value(self):
        return self.quantity * (self.cost_at_writeoff or 0)

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
    # MON-22: yopilishда KUTILGAN naqd SNAPSHOT'i. Ilgari variance() doim
    # jonli hisoblanardi — keyinroq bir sotuv o'chirilса/qaytarilса, oylar
    # oldingi smenning farqi jimgina qayta yozilib, aybni o'sha kassirга
    # ag'darardi. Yopilgан smen uchun shu qotirilgan qiymat ishlatiladi.
    closing_expected_cash = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Yopilish paytida hisoblangan kutilgan naqd (qotirilgan)"
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

    def _txn_qs(self):
        """Bu smenga tegishli cheklar (ARCH-5).

        FK (shift=self) HUKMRON: offline chek kech sinxron bo'lib, sold_at
        vaqti smen oynasidan tashqarida bo'lsa ham o'z smeniga tegishli.
        FK o'rnatilmagan ESKI cheklar uchungina vaqt oynasiga tayanamiz —
        aks holda boshqa smenning cheki bu yerga tushib qolardi.
        """
        end = self.closed_at or timezone.now()
        return SaleTransaction.objects.filter(branch=self.branch).filter(
            models.Q(shift=self) |
            models.Q(shift__isnull=True,
                     sold_at__gte=self.opened_at, sold_at__lt=end))

    def cash_sales(self):
        """Smen davomida kassaga tushgan NAQD sotuvlar.

        Sof naqd cheklar + ARALASH cheklardagi naqd qismi. Aralash naqd ham
        jismonan kassaga tushadi, shuning uchun kutilgan naqd hisobiga kirishi
        SHART (aks holda kassa "ortiq" ko'rinardi). Naqd/Karta/O'tkazma bo'lgan
        bo'linish bilan bir xil qiymat — ko'rsatkichlar mos keladi.
        """
        return self.sales_by_method_split()['cash']

    def sales_by_method(self):
        """Smen davomida to'lov turi bo'yicha savdo: {'cash':.., 'card':.., ...}.

        DIQQAT (ARCH-8): bu funksiya `sales_by_method_split()` DAN FARQ qiladi!
        Bu 'mixed' ni ALOHIDA ustunда qoldiradi (bo'lmaydi); split esa mixedни
        naqd/karta/o'tkazmaga bo'ladi. Kassa hisobiga DOIM split ishlatiladi.
        Bu faqat `total_sales()` uchun (yig'indi ikkalasида ham bir xil).
        Yangi joyда kerak bo'lsa — split'ni tanlang.
        """
        rev_expr = models.ExpressionWrapper(
            models.F('quantity') * models.F('sale_price') - models.F('line_discount'),
            output_field=models.DecimalField(max_digits=14, decimal_places=2))
        txns = self._txn_qs()
        out = {}
        for m, _lbl in SaleTransaction.PaymentMethod.choices:
            ids = list(txns.filter(payment_method=m).values_list('id', flat=True))
            if not ids:
                out[m] = Decimal('0')
                continue
            line_rev = Sale.objects.filter(transaction_id__in=ids).aggregate(
                s=models.Sum(rev_expr))['s'] or Decimal('0')
            odisc = txns.filter(id__in=ids).aggregate(
                s=models.Sum('order_discount'))['s'] or Decimal('0')
            out[m] = _dec(line_rev) - _dec(odisc)
        return out

    def sales_by_method_split(self):
        """To'lov turi bo'yicha — lekin 'aralash' (mixed) cheklar
        payment_breakdown bo'yicha naqd/karta/o'tkazmaga BO'LINADI.

        Har aralash chek KIRITILGAN summalar bo'yicha bo'linadi (nisbatga bo'lish
        EMAS — o'zgartirilgan 2026, yaxlit raqamlar chiqishi uchun)
        (oxirgi qism qoldiqni oladi — yaxlitlash tufayli jami buzilmaydi).
        Faqat {cash, card, transfer} qaytaradi.
        """
        rev_expr = models.ExpressionWrapper(
            models.F('quantity') * models.F('sale_price') - models.F('line_discount'),
            output_field=models.DecimalField(max_digits=14, decimal_places=2))
        txns = self._txn_qs()
        out = {'cash': Decimal('0'), 'card': Decimal('0'), 'transfer': Decimal('0')}
        # Aralash bo'lmagan cheklar — o'z ustuniga
        for m in ('cash', 'card', 'transfer'):
            ids = list(txns.filter(payment_method=m).values_list('id', flat=True))
            if not ids:
                continue
            line_rev = Sale.objects.filter(transaction_id__in=ids).aggregate(
                s=models.Sum(rev_expr))['s'] or Decimal('0')
            odisc = txns.filter(id__in=ids).aggregate(
                s=models.Sum('order_discount'))['s'] or Decimal('0')
            out[m] += _dec(line_rev) - _dec(odisc)
        # Aralash cheklar — payment_breakdown bo'yicha bo'linadi
        mixed = list(txns.filter(payment_method='mixed')
                     .values('id', 'payment_breakdown', 'order_discount'))
        if mixed:
            mids = [t['id'] for t in mixed]
            rev_rows = (Sale.objects.filter(transaction_id__in=mids)
                        .values('transaction_id').annotate(s=models.Sum(rev_expr)))
            rev_map = {r['transaction_id']: _dec(r['s'] or 0) for r in rev_rows}
            for t in mixed:
                net = rev_map.get(t['id'], Decimal('0')) - _dec(t['order_discount'] or 0)
                part = split_breakdown(net, t['payment_breakdown'])  # ARCH-6
                for k in out:
                    out[k] += part[k]
        return out

    def returns_by_method(self):
        """Qaytarilgan pul — ASL sotuvning to'lov turi bo'yicha (chekdan olinadi).

        {cash, card, transfer}. Aralash asl chek — payment_breakdown KIRITILGAN
        summalari bo'yicha bo'linadi (nisbatga bo'lish EMAS). Yig'indi =
        refunds_total (Jami savdo bilan mos keladi).
        Bu 'sof savdo (to'lov turi bo'yicha)' ni ko'rsatish uchun — kassa
        (naqd) hisobiga ta'sir qilmaydi.
        """
        out = {'cash': Decimal('0'), 'card': Decimal('0'), 'transfer': Decimal('0')}
        rets = (Return.objects.filter(shift=self)
                .select_related('sale__transaction'))
        for r in rets:
            amt = _dec(r.effective_cash_refund)
            if amt <= 0:
                continue
            txn = r.sale.transaction if r.sale_id else None
            pm = txn.payment_method if txn else 'cash'
            if pm != 'mixed' or not txn:
                out[_norm_pay_method(pm)] += amt
                continue
            part = split_breakdown(amt, txn.payment_breakdown)  # ARCH-6
            for k in out:
                out[k] += part[k]
        return out

    def total_sales(self):
        """Barcha to'lov turlari bo'yicha jami savdo."""
        return sum(self.sales_by_method().values(), Decimal('0'))

    def payouts_total(self):
        """Smen davomida kassadan olingan naqd (tushlik, xarajat va h.k.)."""
        return self.payouts.aggregate(s=models.Sum('amount'))['s'] or Decimal('0')

    def cash_ins_total(self):
        """Smen davomida kassaga qo'shilgan naqd (MON-4) — payoutning aksi.
        Maydalik (float), egasi qo'ygan pul va h.k. Kutilgan naqdga QO'SHILADI."""
        return self.cash_ins.aggregate(s=models.Sum('amount'))['s'] or Decimal('0')

    def debt_payments_total(self):
        """Smen davomida to'langan xodim qarzlari — kassaga NAQD TUSHADI.

        Xodim ojlik kuni qarzini naqd to'lasa, bu pul kassaga kiradi, demak
        kutilgan naqd shu miqdorga OSHADI.
        """
        return (self.debt_payments.filter(is_paid=True)
                .aggregate(s=models.Sum('amount'))['s'] or Decimal('0'))

    def refunds_total(self):
        """Smen davomida mijozga qaytarilgan pul (kassadan CHIQADI).

        Mahsulot qaytarilganda kassir pulni qaytaradi — demak kutilgan naqd
        shu miqdorga KAMAYADI. Ilgari bu hisobga olinmasdi: qaytarishда dona
        soni tiklanardi, lekin pul aks etmasdi.
        """
        total = Decimal('0')
        for r in Return.objects.filter(shift=self).select_related(
                'sale__transaction'):
            total += _dec(r.effective_cash_refund)
        return total

    def expected_cash(self):
        """Kutilgan naqd = ochilish + naqd sotuvlar + qarz to'lovlari
        + kassaga qo'shilgan naqd − kassadan olingan pul − qaytarilgan pul.

        MON-22: yopilgan smen uchun — YOPILISHDAGI qotirilgan qiymat (keyingi
        tahrirlar tarixni qayta yozmasin). Ochiq smen uchun jonli hisob.
        """
        if self.status == self.Status.CLOSED and self.closing_expected_cash is not None:
            return _dec(self.closing_expected_cash)
        return self.compute_expected_cash()

    def compute_expected_cash(self):
        """Jonli hisoblangan kutilgan naqd (snapshot'siz)."""
        return (_dec(self.opening_cash) + _dec(self.cash_sales())
                + _dec(self.debt_payments_total()) + _dec(self.cash_ins_total())
                - _dec(self.payouts_total()) - _dec(self.refunds_total()))

    def variance(self):
        """Kassa farqi = sanalgan − kutilgan. Manfiy bo'lsa kam, ortiq bo'lsa ko'p."""
        if self.counted_cash is None:
            return None
        return _dec(self.counted_cash) - _dec(self.expected_cash())


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
        constraints = [
            models.CheckConstraint(condition=models.Q(sale_price__gte=0), name='sale_price_nonneg'),
            models.CheckConstraint(condition=models.Q(cost_at_sale__gte=0), name='sale_cost_nonneg'),
            models.CheckConstraint(condition=models.Q(line_discount__gte=0), name='sale_linedisc_nonneg'),
        ]

    @property
    def gross(self):
        """Chegirmasiz summa."""
        return self.quantity * self.sale_price

    @property
    def total(self):
        """Chegirmadan keyingi summa (faqat line_discount)."""
        return self.gross - self.line_discount

    def net_line_total(self):
        """REF-2: chek bo'yicha umumiy chegirma (order_discount) ham ayirilgan
        sof qiymat. order_discount chek qatorlariga nisbatan taqsimlanadi.
        Qaytarish va almashtirish qiymati AYNAN mijoz to'lagani bo'lishi uchun."""
        total = _dec(self.total)
        txn = self.transaction if self.transaction_id else None
        odisc = _dec(txn.order_discount) if txn else Decimal('0')
        if txn and odisc > 0:
            receipt_total = sum((_dec(l.total) for l in txn.lines.all()), Decimal('0'))
            if receipt_total > 0:
                total = total - (odisc * total / receipt_total)
        return total if total > 0 else Decimal('0')

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
        validators=[MinValueValidator(0), MaxValueValidator(100)],  # MON-26
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
    # ----- Almashtirish (exchange) -----
    # Oddiy qaytarishда mijozga to'liq summa naqd qaytariladi. Almashtirishда esa
    # eski tovar qiymati YANGI tovar hisobiga o'tadi — naqd chiqmaydi (yoki faqat
    # farq chiqadi). Shu sabab kassa hisobiga faqat HAQIQIY qaytarilgan naqd
    # (cash_refunded) ta'sir qilishi kerak, to'liq refund_amount emas.
    is_exchange = models.BooleanField(
        default=False,
        help_text='Bu qaytarish almashtirish qismimi (eski tovar yangisiga)')
    cash_refunded = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Almashtirishда mijozga HAQIQIY qaytarilgan naqd (odatda 0 yoki '
                  'faqat farq). None bo\'lsa — oddiy qaytarish, refund_amount ishlatiladi.')
    # S3: ikki marta qaytarishни bloklaydi (timeout/qayta bosishда bir xil kalit).
    idempotency_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True, editable=False,
        help_text='Takroriy qaytarishni bloklovchi bir martalik kalit')

    class Meta:
        verbose_name = 'Qaytarilish'
        verbose_name_plural = 'Qaytarilishlar'
        ordering = ['-refunded_at']
        constraints = [
            # MON-23: manfiy cash_refunded kutilgan naqdни OSHIRib, haqiqiy
            # kamomadni "toza farq"ga aylantirardi — DB darajasида bloklaymiz.
            models.CheckConstraint(
                condition=models.Q(cash_refunded__isnull=True)
                          | models.Q(cash_refunded__gte=0),
                name='return_cash_refunded_nonneg'),
        ]

    def __str__(self):
        return f'Qaytarilish: {self.sale.variant.product.code} × {self.quantity}'

    @property
    def refund_amount(self):
        """Qaytariladigan summa — mijoz HAQIQATDA to'lagani bo'yicha.

        REF-2: chek bo'yicha umumiy chegirma (order_discount) ham hisobga
        olinadi. Ilgari faqat line_discount ayirilardi, order_discount esa
        e'tiborsiz qolib, chegirmali chekni qaytarganда mijoz to'laganidan
        KO'PROQ qaytarilardi (har chegirmali chekда zarar). Chegirmani chek
        qatorlariga nisbatan taqsimlaymiz.
        """
        sale = self.sale
        if sale.quantity <= 0:
            return self.quantity * sale.sale_price
        per_unit_total = sale.net_line_total() / sale.quantity
        return self.quantity * per_unit_total

    @property
    def effective_cash_refund(self):
        """Kassadan HAQIQIY chiqqan naqd.

        Almashtirishда eski tovar qiymati yangi tovar hisobiga o'tadi, shuning
        uchun to'liq refund_amount emas, balki cash_refunded (odatda 0 yoki
        faqat farq) kassaga ta'sir qiladi. Oddiy qaytarishда — to'liq summa.
        """
        if self.is_exchange:
            return self.cash_refunded or Decimal('0')
        return self.refund_amount


class EmployeeDebt(models.Model):
    """Xodim do'kondan ojligacha QARZGA olgan tovar/pul.

    Sotuvga kiritilmaydi — bu alohida daftar. Olinganda ombor qoldig'i kamayadi.
    Ojlik kuni "to'landi" deb belgilanганда naqd KASSAGA TUSHADI (o'zgartirilgan
    2026: debt_payments_total() -> expected_cash oshadi, paid_shift'ga bog'lanadi).
    Ochiq smen bo'lmasa — to'landi bo'ladi, lekin kassaga qo'shilmaydi.
    """
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='employee_debts')
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='debts', help_text='Qarz olgan xodim (ro\'yxatdan)')
    employee_name = models.CharField(
        max_length=120, blank=True,
        help_text='Agar xodim ro\'yxatda bo\'lmasa — ismini yozing')
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 help_text="Qarz summasi (so'm)")
    note = models.CharField(max_length=200, blank=True,
                            help_text='Nima olindi / izoh')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='created_debts')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    is_paid = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='settled_debts')
    paid_shift = models.ForeignKey(
        'Shift', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='debt_payments',
        help_text="Qarz qaysi smen davomida to'landi — kassaga naqd tushishi "
                  "shu smenning kutilgan naqdiга qo'shiladi.")

    class Meta:
        verbose_name = 'Xodim qarzi'
        verbose_name_plural = 'Xodim qarzlari'
        ordering = ['is_paid', '-created_at']

    def __str__(self):
        return f'{self.who}: {self.amount}'

    @property
    def who(self):
        if self.employee:
            return self.employee.get_full_name() or self.employee.username
        return self.employee_name or '—'

    @property
    def item_list(self):
        """Qarzga olingan tovarlar ro'yxati (bo'lsa)."""
        return list(self.items.all())


class EmployeeDebtItem(models.Model):
    """Xodim qarzga olgan bitta tovar qatori.

    Tovar jismonan do'kondan chiqadi — shuning uchun qo'shilganda ombor qoldig'i
    kamayadi (SOTUV emas: kassa/savdoga ta'sir qilmaydi). Qarz o'chirilsa qoldiq
    tiklanadi.
    """
    debt = models.ForeignKey(EmployeeDebt, on_delete=models.CASCADE,
                             related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='employee_debt_items')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL,
                               null=True, blank=True)
    product_name = models.CharField(max_length=200,
                                    help_text='Tovar nomi (snapshot)')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        verbose_name = 'Xodim qarzi — tovar'
        verbose_name_plural = 'Xodim qarzi — tovarlar'

    def __str__(self):
        return f'{self.product_name} × {self.quantity}'

    @property
    def line_total(self):
        return self.quantity * self.unit_price


class ProductRequest(models.Model):
    """Mijoz so'ragan, lekin bizda yo'q (yoki hech sotmagan) mahsulot.

    Sotuvchi shu yerga qayd etadi. Bir xil nom ko'p marta so'ralsa —
    uni omborga kiritish (sotuvni boshlash) uchun signal bo'ladi.
    """
    class Status(models.TextChoices):
        NEW = 'new', 'Kutilmoqda'
        STOCKED = 'stocked', 'Keltirildi'
        DISMISSED = 'dismissed', 'Rad etildi'

    name = models.CharField(
        max_length=200, db_index=True,
        help_text="Mijoz so'ragan mahsulot nomi"
    )
    note = models.CharField(
        max_length=255, blank=True,
        help_text="Qo'shimcha: brend, o'lcham, rang yoki izoh"
    )
    customer_phone = models.CharField(
        max_length=40, blank=True,
        help_text="Mijoz telefoni — mahsulot kelganda xabar berish uchun"
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.NEW
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='product_requests'
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='product_requests'
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='resolved_requests'
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Mijoz so'rovi"
        verbose_name_plural = "Mijoz so'rovlari"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at'],
                         name='inv_prodreq_status_dt'),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_status_display()})'


class CashPayout(models.Model):
    """Kassadan olingan naqd pul (chiqim) — tushlik, do'kon xarajati va h.k.

    Har bir chiqim ochiq smenga bog'lanadi va smen yopilishida
    kutilgan naqddan ayiriladi (kassa farqi to'g'ri chiqishi uchun).
    """
    class Category(models.TextChoices):
        LUNCH = 'lunch', 'Tushlik'
        STORE = 'store', "Do'kon xarajati"
        REPAIR = 'repair', "Ta'mirlash"
        OTHER = 'other', 'Boshqa'

    shift = models.ForeignKey(
        'Shift', on_delete=models.PROTECT, related_name='payouts'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='cash_payouts'
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Kassadan olingan summa (so'm)"
    )
    category = models.CharField(
        max_length=12, choices=Category.choices, default=Category.OTHER
    )
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='cash_payouts'
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    # S5: touchscreen'да ikki marta bosishни bloklaydi (bir martalik kalit).
    idempotency_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True, editable=False)

    #: valid category keys — used to validate incoming POST values
    VALID_CATEGORIES = {c[0] for c in Category.choices}

    class Meta:
        verbose_name = 'Kassa chiqimi'
        verbose_name_plural = 'Kassa chiqimlari'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', '-created_at'],
                         name='cashpayout_branch_dt'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='cashpayout_amount_positive'
            ),
        ]

    def __str__(self):
        return f"{self.amount} so'm — {self.get_category_display()}"


class CashIn(models.Model):
    """Kassaga qo'shilgan naqd pul (kirim) — MON-4.

    Kassadan olish (CashPayout) bor edi, lekin qo'shishнинг yo'li yo'q edi:
    ertalabki maydalik (float), egasi qo'ygan pul, boshqa manbadan tushum —
    hech qayerга yozilmasdi va kassa "ortiq" ko'rinardi. Bu yozuv kutilgan
    naqdga QO'SHILADI (payoutning aksi)."""
    class Category(models.TextChoices):
        FLOAT = 'float', 'Maydalik (float)'
        OWNER = 'owner', 'Egasi qo\'ydi'
        CORRECTION = 'correction', 'Tuzatish'
        OTHER = 'other', 'Boshqa'

    shift = models.ForeignKey('Shift', on_delete=models.PROTECT,
                              related_name='cash_ins')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT,
                               related_name='cash_ins')
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 help_text="Kassaga qo'shilgan summa (so'm)")
    category = models.CharField(max_length=12, choices=Category.choices,
                                default=Category.OTHER)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name='cash_ins')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    # S5: touchscreen'да ikki marta bosishни bloklaydi (bir martalik kalit).
    idempotency_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True, editable=False)

    VALID_CATEGORIES = {c[0] for c in Category.choices}

    class Meta:
        verbose_name = 'Kassa kirimi'
        verbose_name_plural = 'Kassa kirimlari'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', '-created_at'], name='cashin_branch_dt'),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0),
                                   name='cashin_amount_positive'),
        ]

    def __str__(self):
        return f"+{self.amount} so'm — {self.get_category_display()}"


class InvoiceDraft(models.Model):
    """Faktura rasmidan qabul — tugallanmagan ish (qoralama).

    Telefonda suratga olinadi va jadval to'ldiriladi, keyin kompyuterda
    davom ettiriladi. Qoralama omborga TA'SIR QILMAYDI — qabul qilinganda
    o'chiriladi.
    """
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='invoice_drafts'
    )
    supplier_text = models.CharField(max_length=200, blank=True)
    invoice_number = models.CharField(max_length=80, blank=True)
    image = models.ImageField(upload_to='invoices/drafts/', blank=True, null=True)
    payload = models.JSONField(
        default=dict, help_text="Jadval qatorlari va sarlavha maydonlari"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='invoice_drafts'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Faktura qoralamasi'
        verbose_name_plural = 'Faktura qoralamalari'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['-updated_at'], name='invdraft_updated_dt'),
        ]

    def __str__(self):
        return f"Qoralama #{self.pk} — {self.supplier_text or '—'}"

    @property
    def page_count(self):
        return self.pages.count()

    @property
    def row_count(self):
        return len(self.payload.get('rows') or [])

    @property
    def total_qty(self):
        total = 0
        for r in (self.payload.get('rows') or []):
            try:
                total += int(float(r.get('qty') or 0))
            except (TypeError, ValueError):
                pass
        return total


class InvoiceImage(models.Model):
    """Faktura sahifasi — nakladnoy bir necha varaqdan iborat bo'lishi mumkin.

    Bitta rasm ham qoralamaga (hali qabul qilinmagan), ham qabul sessiyasiga
    tegishli bo'lishi mumkin: qoralama qabul qilinganda sahifalar sessiyaga
    ko'chiriladi.
    """
    draft = models.ForeignKey(
        InvoiceDraft, on_delete=models.CASCADE, null=True, blank=True,
        related_name='pages'
    )
    session = models.ForeignKey(
        IntakeSession, on_delete=models.CASCADE, null=True, blank=True,
        related_name='pages'
    )
    image = models.ImageField(upload_to='invoices/pages/')
    order = models.PositiveIntegerField(default=1, help_text='Sahifa raqami')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Faktura sahifasi'
        verbose_name_plural = 'Faktura sahifalari'
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.order}-sahifa'


class QuickSellItem(models.Model):
    """POS 'Tezkor sotuv' toifasi — kodsiz tovar (paypoq, ich kiyim, bosh kiyim).

    Har toifa ochiq narxli mahsulotning alohida turi bo'ladi, shuning uchun
    hisobotda ajralib turadi. Narxlar shu yerda tahrirlanadi.
    """
    name = models.CharField(max_length=60, unique=True,
                            help_text="POS'da tugma nomi (masalan: Paypoq)")
    prices = models.JSONField(
        default=list, blank=True,
        help_text="Narx tugmalari, masalan [2500, 3000, 5000]"
    )
    icon = models.CharField(
        max_length=40, blank=True, default='bi-bag',
        help_text="Bootstrap ikonka klassi (bi-bag, bi-person, bi-handbag)"
    )
    product = models.ForeignKey(
        'Product', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='quick_sell_items',
        help_text="Ombor yuritiladigan mahsulot (Kiyim Kechak kategoriyasida)"
    )
    order = models.PositiveIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Tezkor sotuv toifasi'
        verbose_name_plural = 'Tezkor sotuv toifalari'
        ordering = ['order', 'name']

    def __str__(self):
        return self.name

    @property
    def price_list(self):
        """Tozalangan, tartiblangan narxlar."""
        out = []
        for p in (self.prices or []):
            try:
                v = int(float(p))
            except (TypeError, ValueError):
                continue
            if v > 0 and v not in out:
                out.append(v)
        return sorted(out)

    @property
    def prices_text(self):
        return ', '.join(str(p) for p in self.price_list)


# ---------------------------------------------------------------------------
#  Onlayn do'kon (sayt orqali buyurtmalar)
# ---------------------------------------------------------------------------
class WebOrder(models.Model):
    """Saytdan kelgan buyurtma.

    POS savdosidan (SaleTransaction) ALOHIDA: bu hali sotuv emas, so'rov.
    Tasdiqlangandan keyin xodim uni POS orqali rasmiylashtiradi yoki
    "bajarildi" deb belgilaydi.
    """
    class Status(models.TextChoices):
        NEW = 'new', 'Yangi'
        CONFIRMED = 'confirmed', 'Tasdiqlangan'
        DELIVERED = 'delivered', 'Yetkazildi'
        CANCELLED = 'cancelled', 'Bekor qilindi'

    class Payment(models.TextChoices):
        ON_DELIVERY = 'on_delivery', 'Yetkazib berishda'
        ONLINE = 'online', 'Onlayn to\'lov'

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT,
                               related_name='web_orders')
    customer_name = models.CharField(max_length=120)
    customer_phone = models.CharField(max_length=32)
    address = models.TextField(blank=True)
    note = models.TextField(blank=True)

    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.NEW, db_index=True)
    payment_method = models.CharField(max_length=16, choices=Payment.choices,
                                      default=Payment.ON_DELIVERY)
    is_paid = models.BooleanField(default=False)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name='web_orders')
    handled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Sayt buyurtmasi'
        verbose_name_plural = 'Sayt buyurtmalari'
        ordering = ['-created_at']

    def __str__(self):
        return f'#{self.pk} {self.customer_name} ({self.get_status_display()})'

    @property
    def total_qty(self):
        return sum(l.quantity for l in self.lines.all())


class WebOrderLine(models.Model):
    order = models.ForeignKey(WebOrder, on_delete=models.CASCADE,
                              related_name='lines')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT,
                                related_name='web_order_lines')
    quantity = models.PositiveIntegerField()
    # Narx buyurtma paytida qotiriladi — keyin o'zgarsa ham chek to'g'ri qoladi
    price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = 'Buyurtma qatori'
        verbose_name_plural = 'Buyurtma qatorlari'

    def __str__(self):
        return f'{self.variant} x{self.quantity}'

    @property
    def total(self):
        return self.price * self.quantity
