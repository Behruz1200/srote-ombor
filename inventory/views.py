from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, F, Q, DecimalField, ExpressionWrapper, Count
from django.db.models.functions import Coalesce
from django.db import transaction
from django.http import HttpResponseForbidden, HttpResponse
from django.utils import timezone
from datetime import timedelta, datetime, date
import csv
import io
import re

import qrcode
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)

from .models import (
    User, Branch, Product, ProductVariant, BranchStock,
    Category, Intake, Sale, AuditLog, SaleTransaction, Return,
)
from .forms import (
    LoginForm, BranchForm, ProductForm, CategoryForm,
    IntakeForm, SaleForm, UserCreateForm, UserEditForm, ReportForm,
)


def admin_required(view_func):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_admin():
            return HttpResponseForbidden(
                "<h3>Ruxsat yo'q. Bu sahifa faqat administrator uchun.</h3>"
            )
        return view_func(request, *args, **kwargs)
    return wrapper


def get_user_branch(user):
    """Sotuvchi uchun majburiy filial. Admin uchun None bo'lishi mumkin."""
    return user.branch


def normalize_code(typed):
    """Kiritilgan kodni standart shaklga keltirish: OYO1 → OYO-0001"""
    if not typed:
        return ''
    t = typed.strip().upper().replace(' ', '')
    m = re.match(r'^([A-Z]+)-?(\d+)$', t)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):04d}'
    return t


# ---------- AUTH ----------

def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            messages.success(request, f'Xush kelibsiz, {request.user.username}!')
            return redirect('home')
    else:
        form = LoginForm(request)
    return render(request, 'inventory/login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def home(request):
    if request.user.is_admin():
        return redirect('dashboard')
    return redirect('lookup')


# ---------- LOOKUP (asosiy qidiruv) ----------

@login_required
def lookup(request):
    raw_code = (request.GET.get('code') or '').strip().upper()
    code = normalize_code(raw_code)
    product = None
    branches_data = []
    if code:
        product = Product.objects.filter(code=code).first()
        if product:
            variants = list(product.variants.all())
            sizes = sorted({v.size for v in variants}, key=lambda s: (len(s), s))
            colors = sorted({v.color for v in variants})

            if request.user.is_admin():
                branches = Branch.objects.filter(is_active=True)
            else:
                branches = Branch.objects.filter(pk=request.user.branch_id) \
                    if request.user.branch_id else Branch.objects.none()

            for br in branches:
                stocks = BranchStock.objects.filter(
                    variant__product=product, branch=br
                ).select_related('variant')
                matrix = {(s.variant.size, s.variant.color): s for s in stocks}
                total = sum(s.stock_count for s in stocks)
                branches_data.append({
                    'branch': br, 'matrix': matrix,
                    'sizes': sizes, 'colors': colors, 'total': total,
                })
        else:
            messages.warning(request, f"Kod '{code}' bo'yicha mahsulot topilmadi.")

    return render(request, 'inventory/lookup.html', {
        'code': code, 'product': product, 'branches_data': branches_data,
    })


# ---------- DASHBOARD ----------

@admin_required
def dashboard(request):
    total_products = Product.objects.count()
    total_branches = Branch.objects.filter(is_active=True).count()

    stocks = BranchStock.objects.all()
    total_stock = stocks.aggregate(s=Sum('stock_count'))['s'] or 0
    stock_value = stocks.aggregate(
        v=Sum(ExpressionWrapper(F('stock_count') * F('cost_price'),
                                output_field=DecimalField(max_digits=14, decimal_places=2)))
    )['v'] or 0

    today = timezone.now().date()
    week_ago = today - timedelta(days=7)
    sales_this_week = Sale.objects.filter(sold_at__date__gte=week_ago)
    sales_count = sales_this_week.count()
    sales_revenue = sales_this_week.aggregate(
        s=Sum(ExpressionWrapper(F('quantity') * F('sale_price'),
                                output_field=DecimalField(max_digits=14, decimal_places=2)))
    )['s'] or 0

    # per-branch breakdown
    branch_summary = []
    for br in Branch.objects.filter(is_active=True):
        b_stocks = BranchStock.objects.filter(branch=br)
        b_count = b_stocks.aggregate(s=Sum('stock_count'))['s'] or 0
        b_value = b_stocks.aggregate(
            v=Sum(ExpressionWrapper(F('stock_count') * F('cost_price'),
                                    output_field=DecimalField(max_digits=14, decimal_places=2)))
        )['v'] or 0
        b_sales_week = Sale.objects.filter(branch=br, sold_at__date__gte=week_ago).aggregate(
            s=Sum(ExpressionWrapper(F('quantity') * F('sale_price'),
                                    output_field=DecimalField(max_digits=14, decimal_places=2)))
        )['s'] or 0
        branch_summary.append({
            'branch': br, 'stock': b_count, 'value': b_value, 'week_sales': b_sales_week,
        })

    low_stock = BranchStock.objects.filter(stock_count__lte=3) \
        .select_related('variant__product', 'branch').order_by('stock_count')[:10]
    recent_intakes = Intake.objects.select_related('variant__product', 'branch') \
        .order_by('-received_at')[:10]
    recent_sales = Sale.objects.select_related('variant__product', 'branch') \
        .order_by('-sold_at')[:10]

    return render(request, 'inventory/dashboard.html', {
        'total_products': total_products,
        'total_branches': total_branches,
        'total_stock': total_stock,
        'stock_value': stock_value,
        'sales_count': sales_count,
        'sales_revenue': sales_revenue,
        'branch_summary': branch_summary,
        'low_stock': low_stock,
        'recent_intakes': recent_intakes,
        'recent_sales': recent_sales,
    })


# ---------- PRODUCTS ----------

@admin_required
def product_list(request):
    q = (request.GET.get('q') or '').strip()
    products = Product.objects.all()
    if q:
        products = products.filter(Q(code__icontains=q) | Q(name__icontains=q))
    products = products.select_related('category').prefetch_related('variants__branch_stocks')[:200]
    return render(request, 'inventory/product_list.html', {'products': products, 'q': q})


@admin_required
def product_create(request):
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES)
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Mahsulot yaratildi. Kod: {product.code}")
            return redirect('product_detail', code=product.code)
    else:
        form = ProductForm()
    return render(request, 'inventory/product_form.html', {
        'form': form, 'title': "Yangi mahsulot qo'shish",
    })


@admin_required
def product_detail(request, code):
    product = get_object_or_404(Product, code=normalize_code(code))
    variants = list(product.variants.all())
    sizes = sorted({v.size for v in variants}, key=lambda s: (len(s), s))
    colors = sorted({v.color for v in variants})

    branches_data = []
    for br in Branch.objects.filter(is_active=True):
        stocks = BranchStock.objects.filter(
            variant__product=product, branch=br
        ).select_related('variant')
        matrix = {(s.variant.size, s.variant.color): s for s in stocks}
        total = sum(s.stock_count for s in stocks)
        branches_data.append({
            'branch': br, 'matrix': matrix,
            'sizes': sizes, 'colors': colors, 'total': total,
        })

    recent_intakes = Intake.objects.filter(variant__product=product) \
        .select_related('variant', 'branch', 'received_by').order_by('-received_at')[:20]
    return render(request, 'inventory/product_detail.html', {
        'product': product, 'branches_data': branches_data,
        'recent_intakes': recent_intakes,
    })


@admin_required
def product_edit(request, code):
    product = get_object_or_404(Product, code=normalize_code(code))
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES, instance=product)
        if form.is_valid():
            form.save()
            messages.success(request, 'Mahsulot yangilandi.')
            return redirect('product_detail', code=product.code)
    else:
        form = ProductForm(instance=product)
    return render(request, 'inventory/product_form.html', {
        'form': form, 'title': f'Tahrirlash — {product.name}', 'product': product,
    })


# ---------- INTAKE ----------

@admin_required
def intake_for_product(request, code):
    product = get_object_or_404(Product, code=normalize_code(code))
    if request.method == 'POST':
        form = IntakeForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                cd = form.cleaned_data
                variant, _ = ProductVariant.objects.get_or_create(
                    product=product, size=cd['size'], color=cd['color'],
                )
                stock, created = BranchStock.objects.get_or_create(
                    variant=variant, branch=cd['branch'],
                    defaults={'cost_price': cd['cost_per_unit'],
                              'sale_price': cd['sale_price']},
                )
                stock.stock_count = F('stock_count') + cd['quantity']
                stock.cost_price = cd['cost_per_unit']
                stock.sale_price = cd['sale_price']
                stock.save()
                Intake.objects.create(
                    variant=variant, branch=cd['branch'],
                    quantity=cd['quantity'],
                    cost_per_unit=cd['cost_per_unit'],
                    supplier=cd.get('supplier') or '',
                    note=cd.get('note') or '',
                    received_by=request.user,
                )
                if cd.get('update_product_price'):
                    product.default_sale_price = cd['sale_price']
                    if cd.get('markup_percent') is not None:
                        product.markup_percent = cd['markup_percent']
                    product.save()
                    # Boshqa filiallarda hali sotuv narxi belgilanmagan variantlarni ham yangilash
                    BranchStock.objects.filter(
                        variant__product=product, sale_price=0
                    ).update(sale_price=cd['sale_price'])
            messages.success(request,
                f"Qabul saqlandi: {cd['branch'].name}ga {cd['quantity']} dona qo'shildi. "
                f"Sotuv narxi: {cd['sale_price']:,.0f} so'm.")
            return redirect('product_detail', code=product.code)
    else:
        form = IntakeForm(initial={'markup_percent': product.markup_percent})
    return render(request, 'inventory/intake_form.html', {
        'form': form, 'product': product,
    })


@admin_required
def intake_new(request):
    products = Product.objects.order_by('-created_at')[:50]
    return render(request, 'inventory/intake_choose.html', {'products': products})


# ---------- SALE ----------

@login_required
def sale_create(request, stock_id):
    stock = get_object_or_404(BranchStock.objects.select_related('variant__product', 'branch'),
                              pk=stock_id)
    # Sotuvchilar faqat o'z filialida sotishi mumkin
    if not request.user.is_admin():
        if request.user.branch_id != stock.branch_id:
            return HttpResponseForbidden(
                "<h3>Bu filialda sotishga ruxsat yo'q.</h3>"
            )

    if request.method == 'POST':
        form = SaleForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            qty = cd['quantity']
            if qty > stock.stock_count:
                messages.error(request,
                    f"Omborda yetarli emas. Mavjud: {stock.stock_count}, so'rov: {qty}.")
                return redirect('sale_create', stock_id=stock.id)
            with transaction.atomic():
                stock.stock_count = F('stock_count') - qty
                stock.save()
                Sale.objects.create(
                    variant=stock.variant, branch=stock.branch,
                    quantity=qty, sale_price=cd['sale_price'],
                    cost_at_sale=stock.cost_price,  # snapshot for accurate historical profit
                    note=cd.get('note') or '', sold_by=request.user,
                )
            messages.success(request,
                f"Sotildi: {qty} dona × {cd['sale_price']} so'm ({stock.branch.name}).")
            return redirect('lookup')
    else:
        prefill_price = stock.sale_price if stock.sale_price > 0 else \
            stock.variant.product.default_sale_price
        form = SaleForm(initial={'sale_price': prefill_price})
    return render(request, 'inventory/sale_form.html', {
        'form': form, 'stock': stock,
    })


# ---------- BRANCHES ----------

@admin_required
def branch_list(request):
    branches = Branch.objects.annotate(
        stock_total=Sum('stocks__stock_count'),
        staff_count=Count('staff', distinct=True),
    )
    return render(request, 'inventory/branch_list.html', {'branches': branches})


@admin_required
def branch_create(request):
    if request.method == 'POST':
        form = BranchForm(request.POST)
        if form.is_valid():
            br = form.save()
            messages.success(request, f"Filial yaratildi: {br.name}")
            return redirect('branch_list')
    else:
        form = BranchForm()
    return render(request, 'inventory/branch_form.html', {
        'form': form, 'title': "Yangi filial",
    })


@admin_required
def branch_edit(request, pk):
    branch = get_object_or_404(Branch, pk=pk)
    if request.method == 'POST':
        form = BranchForm(request.POST, instance=branch)
        if form.is_valid():
            form.save()
            messages.success(request, 'Filial yangilandi.')
            return redirect('branch_list')
    else:
        form = BranchForm(instance=branch)
    return render(request, 'inventory/branch_form.html', {
        'form': form, 'title': f'Tahrirlash — {branch.name}', 'branch': branch,
    })


# ---------- USERS ----------

@admin_required
def user_list(request):
    users = User.objects.select_related('branch').order_by('-is_active', 'username')
    return render(request, 'inventory/user_list.html', {'users': users})


@admin_required
def user_create(request):
    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"Foydalanuvchi yaratildi: {user.username}")
            return redirect('user_list')
    else:
        form = UserCreateForm()
    return render(request, 'inventory/user_form.html', {
        'form': form, 'title': "Yangi foydalanuvchi", 'is_create': True,
    })


@admin_required
def user_edit(request, pk):
    target = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        form = UserEditForm(request.POST, instance=target)
        if form.is_valid():
            form.save()
            messages.success(request, 'Foydalanuvchi yangilandi.')
            return redirect('user_list')
    else:
        form = UserEditForm(instance=target)
    return render(request, 'inventory/user_form.html', {
        'form': form, 'title': f'Tahrirlash — {target.username}',
        'target_user': target, 'is_create': False,
    })


# ---------- CATEGORIES ----------

@admin_required
def category_list(request):
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Kategoriya qo'shildi.")
            return redirect('category_list')
    else:
        form = CategoryForm()
    return render(request, 'inventory/category_list.html', {
        'categories': Category.objects.all(), 'form': form,
    })


# ---------- SALES LIST ----------

@admin_required
def sales_list(request):
    # Annotate returned_qty so the template doesn't trigger N+1
    sales = (Sale.objects
        .select_related('variant__product', 'branch', 'sold_by')
        .annotate(_returned=Coalesce(Sum('returns__quantity'), 0))
        .order_by('-sold_at')[:200])
    total = sum(s.total for s in sales)
    return render(request, 'inventory/sales_list.html', {'sales': sales, 'total': total})


# ---------- REPORTS ----------

def _resolve_period(period, date_from, date_to):
    """Davr bo'yicha (start_dt, end_dt) qaytaradi. end_dt = davrdan keyingi kun."""
    today = timezone.localdate()
    if period == 'today':
        start = today
        end = today + timedelta(days=1)
    elif period == 'week':
        start = today - timedelta(days=7)
        end = today + timedelta(days=1)
    elif period == 'month':
        start = today - timedelta(days=30)
        end = today + timedelta(days=1)
    elif period == 'this_month':
        start = today.replace(day=1)
        end = today + timedelta(days=1)
    elif period == 'custom':
        start = date_from or today
        end = (date_to or today) + timedelta(days=1)
    else:
        start = today - timedelta(days=7)
        end = today + timedelta(days=1)
    tz = timezone.get_current_timezone()
    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=tz)
    end_dt = datetime.combine(end, datetime.min.time()).replace(tzinfo=tz)
    return start, end - timedelta(days=1), start_dt, end_dt


@admin_required
def reports(request):
    form = ReportForm(request.GET or None, initial={'period': 'week', 'report_type': 'sales'})
    rows = []
    headers = []
    title = ''
    summary = {}

    if form.is_valid():
        rtype = form.cleaned_data['report_type']
        period = form.cleaned_data['period']
        date_from = form.cleaned_data.get('date_from')
        date_to = form.cleaned_data.get('date_to')
        branch = form.cleaned_data.get('branch')

        d_start, d_end, dt_start, dt_end = _resolve_period(period, date_from, date_to)

        if rtype == 'sales':
            title = "Sotuvlar hisoboti"
            qs = Sale.objects.filter(sold_at__gte=dt_start, sold_at__lt=dt_end) \
                .select_related('variant__product', 'branch', 'sold_by').order_by('-sold_at')
            if branch:
                qs = qs.filter(branch=branch)
            headers = ['Sana', 'Filial', 'Kod', 'Mahsulot', "O'lcham", 'Rang',
                       'Soni', "Narx (so'm)", "Jami (so'm)", 'Sotuvchi', 'Izoh']
            total_qty = 0
            total_rev = 0
            for s in qs:
                rows.append([
                    timezone.localtime(s.sold_at).strftime('%Y-%m-%d %H:%M'),
                    s.branch.name, s.variant.product.code, s.variant.product.name,
                    s.variant.size, s.variant.color, s.quantity,
                    s.sale_price, s.total, s.sold_by.username, s.note,
                ])
                total_qty += s.quantity
                total_rev += s.total
            summary = {'Jami sotilgan dona': total_qty,
                       "Jami daromad (so'm)": total_rev,
                       'Sotuvlar soni': qs.count()}

        elif rtype == 'intakes':
            title = "Qabullar hisoboti"
            qs = Intake.objects.filter(received_at__gte=dt_start, received_at__lt=dt_end) \
                .select_related('variant__product', 'branch', 'received_by') \
                .order_by('-received_at')
            if branch:
                qs = qs.filter(branch=branch)
            headers = ['Sana', 'Filial', 'Kod', 'Mahsulot', "O'lcham", 'Rang',
                       'Soni', "Tannarx (so'm)", "Jami xarajat (so'm)",
                       'Yetkazib beruvchi', 'Qabul qildi', 'Izoh']
            total_qty = 0
            total_cost = 0
            for i in qs:
                rows.append([
                    timezone.localtime(i.received_at).strftime('%Y-%m-%d %H:%M'),
                    i.branch.name, i.variant.product.code, i.variant.product.name,
                    i.variant.size, i.variant.color, i.quantity,
                    i.cost_per_unit, i.total_cost, i.supplier,
                    i.received_by.username, i.note,
                ])
                total_qty += i.quantity
                total_cost += i.total_cost
            summary = {'Jami qabul qilingan dona': total_qty,
                       "Jami xarajat (so'm)": total_cost,
                       'Qabullar soni': qs.count()}

        elif rtype == 'inventory':
            title = "Joriy ombor holati"
            qs = BranchStock.objects.filter(stock_count__gt=0) \
                .select_related('variant__product', 'branch') \
                .order_by('branch__name', 'variant__product__name')
            if branch:
                qs = qs.filter(branch=branch)
            headers = ['Filial', 'Kod', 'Mahsulot', "O'lcham", 'Rang',
                       'Soni', "Tannarx (so'm)", "Jami qiymat (so'm)"]
            total_qty = 0
            total_val = 0
            for s in qs:
                val = s.stock_count * s.cost_price
                rows.append([
                    s.branch.name, s.variant.product.code, s.variant.product.name,
                    s.variant.size, s.variant.color,
                    s.stock_count, s.cost_price, val,
                ])
                total_qty += s.stock_count
                total_val += val
            summary = {'Jami dona': total_qty, "Jami qiymat (so'm)": total_val,
                       'Variantlar soni': qs.count()}

        elif rtype == 'by_product':
            title = "Mahsulotlar bo'yicha sotuv xulosasi"
            sale_qs = Sale.objects.filter(sold_at__gte=dt_start, sold_at__lt=dt_end)
            if branch:
                sale_qs = sale_qs.filter(branch=branch)
            agg = sale_qs.values(
                'variant__product__code', 'variant__product__name'
            ).annotate(
                qty=Sum('quantity'),
                revenue=Sum(ExpressionWrapper(
                    F('quantity') * F('sale_price'),
                    output_field=DecimalField(max_digits=14, decimal_places=2))),
                count=Count('id'),
            ).order_by('-revenue')
            headers = ['Kod', 'Mahsulot', 'Sotuvlar soni', 'Sotilgan dona', "Daromad (so'm)"]
            total_qty = 0
            total_rev = 0
            for a in agg:
                rows.append([
                    a['variant__product__code'], a['variant__product__name'],
                    a['count'], a['qty'], a['revenue'],
                ])
                total_qty += a['qty'] or 0
                total_rev += a['revenue'] or 0
            summary = {'Jami sotilgan dona': total_qty,
                       "Jami daromad (so'm)": total_rev,
                       'Mahsulotlar soni': len(agg)}

        if request.GET.get('export') == 'csv':
            return _csv_response(title, headers, rows, summary,
                                 d_start, d_end, branch)
        if request.GET.get('export') == 'pdf':
            return _pdf_response(title, headers, rows, summary,
                                 d_start, d_end, branch)

        return render(request, 'inventory/reports.html', {
            'form': form, 'rows': rows, 'headers': headers, 'title': title,
            'summary': summary, 'd_start': d_start, 'd_end': d_end,
            'branch': branch,
        })

    return render(request, 'inventory/reports.html', {
        'form': form, 'rows': None, 'headers': [], 'title': '',
    })


# ---------- CART / MULTI-ITEM SALE ----------

CART_KEY = 'cart'  # session dict: {str(stock_id): qty}


def _get_cart(request):
    return request.session.get(CART_KEY, {}) or {}


def _save_cart(request, cart):
    # drop zero/negative entries
    cart = {k: int(v) for k, v in cart.items() if int(v) > 0}
    request.session[CART_KEY] = cart
    request.session.modified = True
    return cart


def _cart_lines(cart):
    """Resolve cart {stock_id: qty} → list of {stock, qty, available, ok}."""
    if not cart:
        return []
    stock_ids = [int(sid) for sid in cart.keys() if str(sid).isdigit()]
    stocks = {bs.id: bs for bs in BranchStock.objects.filter(id__in=stock_ids)
              .select_related('variant__product', 'branch')}
    lines = []
    for sid, qty in cart.items():
        stock = stocks.get(int(sid))
        if not stock:
            continue
        lines.append({
            'stock': stock,
            'qty': int(qty),
            'available': stock.stock_count,
            'ok': int(qty) <= stock.stock_count,
            'subtotal': int(qty) * (stock.sale_price or stock.variant.product.default_sale_price),
        })
    return lines


def cart_count(request):
    """Helper used by base.html navbar to show cart badge."""
    cart = _get_cart(request)
    return sum(int(v) for v in cart.values())


@login_required
def cart_add(request, stock_id):
    """POST /cart/add/<stock_id>/  with qty (default 1)."""
    stock = get_object_or_404(BranchStock.objects.select_related('branch'), pk=stock_id)
    if not request.user.is_admin() and request.user.branch_id != stock.branch_id:
        return HttpResponseForbidden("Bu filialda sotishga ruxsat yo'q.")

    try:
        qty = max(1, int(request.POST.get('qty') or 1))
    except ValueError:
        qty = 1
    cart = _get_cart(request)
    new_qty = int(cart.get(str(stock_id), 0)) + qty
    if new_qty > stock.stock_count:
        new_qty = stock.stock_count
        messages.warning(request,
            f"Omborda faqat {stock.stock_count} dona bor.")
    cart[str(stock_id)] = new_qty
    _save_cart(request, cart)
    messages.success(request,
        f"Savatga qo'shildi: {stock.variant.product.code} × {qty} dona.")
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or 'lookup'
    return redirect(next_url if next_url.startswith('/') else 'lookup')


@login_required
def cart_view(request):
    cart = _get_cart(request)
    lines = _cart_lines(cart)
    # If user is a seller, only their branch's items
    if not request.user.is_admin():
        lines = [l for l in lines if l['stock'].branch_id == request.user.branch_id]
    total = sum(l['subtotal'] for l in lines)
    return render(request, 'inventory/cart.html', {
        'lines': lines, 'total': total,
        'payment_methods': SaleTransaction.PaymentMethod.choices,
    })


@login_required
def cart_update(request):
    """POST: update qty of an item or remove it."""
    cart = _get_cart(request)
    stock_id = request.POST.get('stock_id')
    action = request.POST.get('action')
    if stock_id in cart:
        if action == 'remove':
            del cart[stock_id]
        else:
            try:
                qty = int(request.POST.get('qty') or 0)
            except ValueError:
                qty = 0
            if qty <= 0:
                del cart[stock_id]
            else:
                stock = BranchStock.objects.filter(pk=stock_id).first()
                if stock:
                    cart[stock_id] = min(qty, stock.stock_count)
    _save_cart(request, cart)
    return redirect('cart_view')


@login_required
def cart_clear(request):
    request.session[CART_KEY] = {}
    request.session.modified = True
    messages.info(request, "Savat bo'shatildi.")
    return redirect('lookup')


@login_required
def checkout(request):
    cart = _get_cart(request)
    lines = _cart_lines(cart)
    if not request.user.is_admin():
        lines = [l for l in lines if l['stock'].branch_id == request.user.branch_id]
    if not lines:
        messages.warning(request, "Savat bo'sh.")
        return redirect('lookup')

    # All lines must be from same branch
    branches = {l['stock'].branch_id for l in lines}
    if len(branches) > 1:
        messages.error(request,
            "Bitta chekda turli filiallardan tovar bo'lmaydi. Ortiqcha tovarlarni olib tashlang.")
        return redirect('cart_view')
    if any(not l['ok'] for l in lines):
        messages.error(request, "Ba'zi tovarlar yetarli emas — savatni tekshiring.")
        return redirect('cart_view')

    if request.method == 'POST':
        with transaction.atomic():
            branch = lines[0]['stock'].branch
            txn = SaleTransaction.objects.create(
                branch=branch,
                sold_by=request.user,
                payment_method=request.POST.get('payment_method') or 'cash',
                customer_name=request.POST.get('customer_name') or '',
                customer_phone=request.POST.get('customer_phone') or '',
                note=request.POST.get('note') or '',
            )
            for line in lines:
                stock = line['stock']
                qty = line['qty']
                stock.stock_count = F('stock_count') - qty
                stock.save()
                Sale.objects.create(
                    transaction=txn,
                    variant=stock.variant, branch=stock.branch,
                    quantity=qty,
                    sale_price=stock.sale_price or stock.variant.product.default_sale_price,
                    cost_at_sale=stock.cost_price,
                    sold_by=request.user,
                )
            request.session[CART_KEY] = {}
            request.session.modified = True
        messages.success(request,
            f"Sotuv yakunlandi: {len(lines)} ta mahsulot.")
        return redirect('transaction_detail', pk=txn.pk)

    return render(request, 'inventory/checkout.html', {
        'lines': lines, 'total': sum(l['subtotal'] for l in lines),
        'payment_methods': SaleTransaction.PaymentMethod.choices,
    })


@login_required
def transaction_detail(request, pk):
    txn = get_object_or_404(
        SaleTransaction.objects.select_related('branch', 'sold_by')
            .prefetch_related('lines__variant__product'),
        pk=pk,
    )
    if not request.user.is_admin() and request.user.branch_id != txn.branch_id:
        return HttpResponseForbidden("Bu chekni ko'rishga ruxsat yo'q.")
    return render(request, 'inventory/transaction_detail.html', {'txn': txn})


# ---------- RETURNS ----------

@login_required
def return_create(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related('variant__product', 'branch'),
                             pk=sale_id)
    if not request.user.is_admin() and request.user.branch_id != sale.branch_id:
        return HttpResponseForbidden()

    max_returnable = sale.quantity - sale.returned_qty
    if max_returnable <= 0:
        messages.warning(request, "Bu sotuv allaqachon to'liq qaytarilgan.")
        return redirect('sales_list')

    if request.method == 'POST':
        try:
            qty = int(request.POST.get('quantity') or 0)
        except ValueError:
            qty = 0
        reason = (request.POST.get('reason') or '').strip()
        if qty < 1 or qty > max_returnable:
            messages.error(request,
                f"Qaytarish miqdori 1 dan {max_returnable} gacha bo'lishi kerak.")
            return redirect('return_create', sale_id=sale.id)
        with transaction.atomic():
            stock = BranchStock.objects.filter(
                variant=sale.variant, branch=sale.branch
            ).first()
            if stock:
                stock.stock_count = F('stock_count') + qty
                stock.save()
            Return.objects.create(
                sale=sale, quantity=qty, reason=reason,
                refunded_by=request.user,
            )
        messages.success(request,
            f"Qaytarildi: {qty} dona × {sale.sale_price} so'm.")
        return redirect('sales_list')

    return render(request, 'inventory/return_form.html', {
        'sale': sale, 'max_returnable': max_returnable,
    })


# ---------- AUDIT LOG ----------

@admin_required
def audit_list(request):
    logs = AuditLog.objects.select_related('user').all()
    # Filters
    action = request.GET.get('action') or ''
    user_filter = request.GET.get('user') or ''
    model = request.GET.get('model') or ''
    q = (request.GET.get('q') or '').strip()
    if action:
        logs = logs.filter(action=action)
    if user_filter:
        logs = logs.filter(username_snapshot=user_filter)
    if model:
        logs = logs.filter(model_name=model)
    if q:
        logs = logs.filter(
            Q(username_snapshot__icontains=q) |
            Q(object_repr__icontains=q) |
            Q(model_name__icontains=q) |
            Q(object_id=q)
        )

    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(logs, 50)
    page = paginator.get_page(request.GET.get('page'))

    actions = AuditLog.Action.choices
    users = (AuditLog.objects.values_list('username_snapshot', flat=True)
             .distinct().order_by('username_snapshot'))
    users = [u for u in users if u]
    models_list = (AuditLog.objects.values_list('model_name', flat=True)
                   .distinct().order_by('model_name'))
    models_list = [m for m in models_list if m]

    return render(request, 'inventory/audit_list.html', {
        'page': page, 'actions': actions, 'users': users,
        'models_list': models_list,
        'f_action': action, 'f_user': user_filter, 'f_model': model, 'q': q,
    })


# ---------- PWA ----------

def _serve_static_file(rel_path, content_type):
    from django.conf import settings
    import os
    path = os.path.join(settings.BASE_DIR, 'static', rel_path)
    if not os.path.exists(path):
        return HttpResponse(status=404)
    with open(path, 'rb') as f:
        data = f.read()
    response = HttpResponse(data, content_type=content_type)
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def manifest(request):
    """PWA manifest served at /manifest.webmanifest"""
    return _serve_static_file('manifest.webmanifest', 'application/manifest+json')


def service_worker(request):
    """PWA service worker — must be served at root scope."""
    response = _serve_static_file('sw.js', 'application/javascript')
    response['Service-Worker-Allowed'] = '/'
    return response


# ---------- QR / ETIKETKA ----------

@login_required
def qr_image(request, code):
    """Mahsulot kodi uchun QR PNG"""
    norm = normalize_code(code)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8, border=2,
    )
    qr.add_data(norm)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    response = HttpResponse(buf.getvalue(), content_type='image/png')
    response['Cache-Control'] = 'public, max-age=3600'
    return response


@admin_required
def product_labels(request, code=None):
    """A4 print etiketka sahifasi"""
    if code:
        products = [get_object_or_404(Product, code=normalize_code(code))]
    else:
        ids = request.GET.getlist('id') or request.GET.get('id', '').split(',')
        ids = [i for i in ids if i.strip().isdigit()]
        if ids:
            products = list(Product.objects.filter(id__in=ids))
        else:
            products = []
    try:
        copies = max(1, min(50, int(request.GET.get('copies', 6))))
    except ValueError:
        copies = 6
    items = []
    for p in products:
        for _ in range(copies):
            items.append(p)
    return render(request, 'inventory/labels.html', {
        'items': items, 'products': products, 'copies': copies,
    })


# ---------- INSIGHTS / ANALYTICS ----------

def _insights_context(request):
    """Insights view va export uchun barcha hisoblar."""
    period = request.GET.get('period', 'month')
    branch_id = request.GET.get('branch') or ''

    today = timezone.localdate()
    if period == 'week':
        d_start = today - timedelta(days=7)
        days = 7
    elif period == 'today':
        d_start = today
        days = 1
    elif period == 'quarter':
        d_start = today - timedelta(days=90)
        days = 90
    else:
        period = 'month'
        d_start = today - timedelta(days=30)
        days = 30

    tz = timezone.get_current_timezone()
    dt_start = datetime.combine(d_start, datetime.min.time()).replace(tzinfo=tz)
    dt_end = datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(tzinfo=tz)
    # Oldingi davr (taqqoslash uchun)
    prev_d_start = d_start - timedelta(days=days)
    prev_dt_start = datetime.combine(prev_d_start, datetime.min.time()).replace(tzinfo=tz)
    prev_dt_end = dt_start

    sales = Sale.objects.filter(sold_at__gte=dt_start, sold_at__lt=dt_end)
    branches_all = Branch.objects.filter(is_active=True)
    selected_branch = None
    if branch_id:
        try:
            selected_branch = Branch.objects.get(pk=int(branch_id))
            sales = sales.filter(branch=selected_branch)
        except (Branch.DoesNotExist, ValueError):
            pass

    revenue_expr = ExpressionWrapper(
        F('quantity') * F('sale_price'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )

    # Umumiy KPI
    totals = sales.aggregate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
        sales_count=Count('id'),
    )
    revenue = totals['revenue'] or 0
    qty = totals['qty'] or 0
    sales_count = totals['sales_count'] or 0

    # Foyda hisoblash: revenue - cost. cost = sum(quantity * variant.branch_stock.cost_price)
    # Sale modelida cost_price yo'q, lekin oxirgi BranchStock dan oladi (taxmin).
    # Aniqroq bo'lishi uchun har sotuv pay'tida cost_price'ni Sale'da saqlash kerak edi;
    # hozircha BranchStock.cost_price'dan foydalanamiz.
    # Cost is snapshotted on the Sale itself (cost_at_sale), so we can aggregate in SQL.
    cost_expr = ExpressionWrapper(
        F('quantity') * F('cost_at_sale'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    total_cost = sales.aggregate(c=Sum(cost_expr))['c'] or 0

    profit_by_product_qs = sales.values(
        'variant__product_id', 'variant__product__code', 'variant__product__name',
    ).annotate(
        revenue=Sum(revenue_expr),
        cost=Sum(cost_expr),
        qty=Sum('quantity'),
    )
    profit_by_product = {}
    for row in profit_by_product_qs:
        profit_by_product[row['variant__product_id']] = {
            'code': row['variant__product__code'],
            'name': row['variant__product__name'],
            'revenue': row['revenue'] or 0,
            'cost': row['cost'] or 0,
            'profit': (row['revenue'] or 0) - (row['cost'] or 0),
            'qty': row['qty'] or 0,
        }

    profit = revenue - total_cost
    margin = (profit / revenue * 100) if revenue else 0

    # TOP foyda keltirgan mahsulotlar (revenue emas, balki real foyda)
    top_profit = sorted(profit_by_product.values(),
                        key=lambda x: x['profit'], reverse=True)[:10]
    for p in top_profit:
        p['margin'] = (p['profit'] / p['revenue'] * 100) if p['revenue'] else 0

    # O'rtacha sotuv summasi va eng katta sotuv
    avg_sale_value = (revenue / sales_count) if sales_count else 0
    largest_sale = sales.annotate(
        line=ExpressionWrapper(F('quantity') * F('sale_price'),
                               output_field=DecimalField(max_digits=14, decimal_places=2))
    ).order_by('-line').first()

    # O'tgan davr taqqoslash (week-over-week / period-over-period)
    prev_sales = Sale.objects.filter(sold_at__gte=prev_dt_start, sold_at__lt=prev_dt_end)
    if branch_id and selected_branch:
        prev_sales = prev_sales.filter(branch=selected_branch)
    prev_totals = prev_sales.aggregate(
        revenue=Sum(revenue_expr), qty=Sum('quantity'), n=Count('id'),
    )
    prev_revenue = prev_totals['revenue'] or 0
    prev_qty = prev_totals['qty'] or 0
    prev_n = prev_totals['n'] or 0

    def growth(curr, prev):
        if not prev:
            return None
        return float((curr - prev) / prev * 100)

    rev_growth = growth(float(revenue), float(prev_revenue))
    qty_growth = growth(qty, prev_qty)
    sales_growth = growth(sales_count, prev_n)

    # Filiallar taqqoslash (current period)
    branch_compare = []
    for br in branches_all:
        b_sales = sales.filter(branch=br)
        b_agg = b_sales.aggregate(
            rev=Sum(revenue_expr), cost=Sum(cost_expr),
            qty=Sum('quantity'), n=Count('id'),
        )
        b_rev = b_agg['rev'] or 0
        b_cost = b_agg['cost'] or 0
        b_profit = b_rev - b_cost
        b_n = b_agg['n'] or 0
        b_qty = b_agg['qty'] or 0
        b_stock = BranchStock.objects.filter(branch=br).aggregate(
            s=Sum('stock_count'))['s'] or 0
        branch_compare.append({
            'branch': br,
            'revenue': b_rev,
            'cost': b_cost,
            'profit': b_profit,
            'margin': (b_profit / b_rev * 100) if b_rev else 0,
            'sales_count': b_n,
            'qty': b_qty,
            'avg_sale': (b_rev / b_n) if b_n else 0,
            'stock': b_stock,
        })

    # Tovar aylanmasi: kunlik o'rtacha sotuv va omborda qancha kun yetadi
    # Top sotilgan mahsulotlar uchun
    turnover = []
    for pid, pdata in sorted(profit_by_product.items(),
                             key=lambda x: x[1]['qty'], reverse=True)[:10]:
        daily_avg = pdata['qty'] / days if days else 0
        current_stock = BranchStock.objects.filter(
            variant__product_id=pid
        ).aggregate(s=Sum('stock_count'))['s'] or 0
        days_left = (current_stock / daily_avg) if daily_avg else None
        turnover.append({
            'code': pdata['code'], 'name': pdata['name'],
            'daily_avg': daily_avg, 'stock': current_stock,
            'days_left': days_left,
        })

    # TOP 10 mahsulot (qty va daromad bo'yicha)
    top_products = list(sales.values(
        'variant__product__id', 'variant__product__code',
        'variant__product__name',
    ).annotate(
        qty=Sum('quantity'),
        revenue=Sum(revenue_expr),
        n_sales=Count('id'),
    ).order_by('-revenue')[:10])

    # SLOW MOVERS — bu davrda umuman sotilmagan mahsulotlar
    sold_product_ids = sales.values_list('variant__product_id', flat=True).distinct()
    slow_products = list(
        Product.objects.exclude(id__in=sold_product_ids)
        .select_related('category')
        .annotate(stock=Sum('variants__branch_stocks__stock_count'))[:15]
    )

    # TOP sotuvchilar
    top_sellers = list(sales.values(
        'sold_by__id', 'sold_by__username',
        'sold_by__first_name', 'sold_by__last_name',
        'sold_by__branch__name',
    ).annotate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
        n_sales=Count('id'),
    ).order_by('-revenue')[:10])

    # Filiallar bo'yicha
    by_branch = list(sales.values(
        'branch__id', 'branch__name'
    ).annotate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
        n_sales=Count('id'),
    ).order_by('-revenue'))

    # Kategoriya bo'yicha
    by_category = list(sales.values(
        'variant__product__category__name',
    ).annotate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
    ).order_by('-revenue'))

    # Sotuv trendi (kunlar bo'yicha)
    daily_map = {}
    for i in range(days):
        d = d_start + timedelta(days=i)
        daily_map[d.isoformat()] = {'qty': 0, 'revenue': 0}
    for s in sales:
        day_key = timezone.localtime(s.sold_at).date().isoformat()
        if day_key in daily_map:
            daily_map[day_key]['qty'] += s.quantity
            daily_map[day_key]['revenue'] += float(s.quantity * s.sale_price)
    daily_labels = list(daily_map.keys())
    daily_revenue = [daily_map[k]['revenue'] for k in daily_labels]
    daily_qty = [daily_map[k]['qty'] for k in daily_labels]

    # Soatlar bo'yicha (0-23)
    hour_buckets = [0] * 24
    for s in sales:
        h = timezone.localtime(s.sold_at).hour
        hour_buckets[h] += s.quantity

    # Hafta kunlari bo'yicha
    weekday_labels = ['Du', 'Se', 'Ch', 'Pa', 'Ju', 'Sh', 'Ya']
    weekday_buckets = [0] * 7
    for s in sales:
        wd = timezone.localtime(s.sold_at).weekday()  # 0=Monday
        weekday_buckets[wd] += s.quantity

    # Ombor risk: sof aksiya, narx
    out_of_stock = list(
        BranchStock.objects.filter(stock_count=0)
        .select_related('variant__product', 'branch')[:20]
    )

    # Foyda yuqori mahsulotlar (markup bo'yicha eng yuqori)
    high_margin = list(
        Product.objects.order_by('-markup_percent')[:8]
    )

    context = {
        'period': period,
        'd_start': d_start,
        'd_end': today,
        'days': days,
        'selected_branch': selected_branch,
        'branches_all': branches_all,
        'revenue': revenue,
        'qty': qty,
        'sales_count': sales_count,
        'total_cost': total_cost,
        'profit': profit,
        'margin': margin,
        'avg_sale_value': avg_sale_value,
        'largest_sale': largest_sale,
        'prev_revenue': prev_revenue,
        'prev_qty': prev_qty,
        'prev_n': prev_n,
        'rev_growth': rev_growth,
        'qty_growth': qty_growth,
        'sales_growth': sales_growth,
        'top_products': top_products,
        'top_profit': top_profit,
        'slow_products': slow_products,
        'top_sellers': top_sellers,
        'by_branch': by_branch,
        'by_category': by_category,
        'branch_compare': branch_compare,
        'turnover': turnover,
        'daily_labels': daily_labels,
        'daily_revenue': daily_revenue,
        'daily_qty': daily_qty,
        'hour_buckets': hour_buckets,
        'weekday_labels': weekday_labels,
        'weekday_buckets': weekday_buckets,
        'out_of_stock': out_of_stock,
        'high_margin': high_margin,
    }
    return context


@admin_required
def insights(request):
    ctx = _insights_context(request)
    fmt = request.GET.get('export', '').lower()
    if fmt == 'csv':
        return _insights_csv(ctx)
    if fmt == 'pdf':
        return _insights_pdf(ctx)
    return render(request, 'inventory/insights.html', ctx)


def _insights_csv(ctx):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    fn = f"Biznes_tahlili_{ctx['d_start']}_{ctx['d_end']}.csv"
    response['Content-Disposition'] = f'attachment; filename="{fn}"'
    response.write('﻿')
    w = csv.writer(response)
    branch_str = ctx['selected_branch'].name if ctx['selected_branch'] else 'Barcha filiallar'
    w.writerow([f"Srote — Biznes tahlili"])
    w.writerow([f"Davr: {ctx['d_start']} — {ctx['d_end']}  ({ctx['days']} kun)"])
    w.writerow([f"Filial: {branch_str}"])
    w.writerow([])

    w.writerow(['ASOSIY KO\'RSATKICHLAR'])
    w.writerow(["Daromad (so'm)", ctx['revenue']])
    w.writerow(["Tannarx (so'm)", ctx['total_cost']])
    w.writerow(["Sof foyda (so'm)", ctx['profit']])
    w.writerow(['Foyda marjasi (%)', f"{ctx['margin']:.2f}"])
    w.writerow(['Sotuvlar soni', ctx['sales_count']])
    w.writerow(['Sotilgan dona', ctx['qty']])
    w.writerow(["O'rtacha sotuv summasi (so'm)", round(ctx['avg_sale_value'])])
    if ctx['largest_sale']:
        w.writerow(["Eng katta bitta sotuv (so'm)", ctx['largest_sale'].line])
    w.writerow([])

    w.writerow(["OLDINGI DAVR BILAN TAQQOSLASH"])
    w.writerow(['Ko\'rsatkich', 'Hozir', 'Oldin', "O'sish %"])
    g = lambda v: f"{v:+.1f}%" if v is not None else '—'
    w.writerow(['Daromad', ctx['revenue'], ctx['prev_revenue'], g(ctx['rev_growth'])])
    w.writerow(['Dona', ctx['qty'], ctx['prev_qty'], g(ctx['qty_growth'])])
    w.writerow(['Sotuvlar', ctx['sales_count'], ctx['prev_n'], g(ctx['sales_growth'])])
    w.writerow([])

    w.writerow([f"TOP-10 MAHSULOT (DAROMAD BO'YICHA)"])
    w.writerow(['Kod', 'Mahsulot', 'Dona', 'Sotuvlar', "Daromad (so'm)"])
    for p in ctx['top_products']:
        w.writerow([p['variant__product__code'], p['variant__product__name'],
                    p['qty'], p['n_sales'], p['revenue']])
    w.writerow([])

    w.writerow([f"TOP-10 MAHSULOT (SOF FOYDA BO'YICHA)"])
    w.writerow(['Kod', 'Mahsulot', 'Dona', "Daromad", "Tannarx", "Foyda", 'Marja %'])
    for p in ctx['top_profit']:
        w.writerow([p['code'], p['name'], p['qty'], p['revenue'],
                    p['cost'], p['profit'], f"{p['margin']:.1f}"])
    w.writerow([])

    w.writerow(["SOTILMAYOTGAN MAHSULOTLAR"])
    w.writerow(['Kod', 'Mahsulot', 'Kategoriya', 'Omborda dona'])
    for p in ctx['slow_products']:
        w.writerow([p.code, p.name,
                    p.category.name if p.category else '—',
                    p.stock or 0])
    w.writerow([])

    w.writerow(["FILIALLAR TAQQOSLASH"])
    w.writerow(['Filial', "Daromad", "Tannarx", "Foyda", 'Marja %',
                'Sotuvlar', 'Dona', "O'rtacha sotuv", 'Omborda dona'])
    for b in ctx['branch_compare']:
        w.writerow([b['branch'].name, b['revenue'], b['cost'], b['profit'],
                    f"{b['margin']:.1f}", b['sales_count'], b['qty'],
                    round(b['avg_sale']), b['stock']])
    w.writerow([])

    w.writerow(["TOP-10 SOTUVCHILAR"])
    w.writerow(['Username', 'Ism', 'Filial', 'Sotuvlar', 'Dona', "Daromad (so'm)"])
    for s in ctx['top_sellers']:
        w.writerow([
            s['sold_by__username'],
            f"{s['sold_by__first_name'] or ''} {s['sold_by__last_name'] or ''}".strip(),
            s['sold_by__branch__name'] or '—',
            s['n_sales'], s['qty'], s['revenue'],
        ])
    w.writerow([])

    w.writerow(["TOVAR AYLANMASI (Top sotilganlar)"])
    w.writerow(['Kod', 'Mahsulot', 'Kunlik o\'rtacha (dona)', 'Hozir omborda', 'Necha kun yetadi'])
    for t in ctx['turnover']:
        dleft = f"{t['days_left']:.0f}" if t['days_left'] is not None else '—'
        w.writerow([t['code'], t['name'], f"{t['daily_avg']:.2f}", t['stock'], dleft])
    w.writerow([])

    w.writerow(["OMBORDA TUGAGAN"])
    w.writerow(['Filial', 'Kod', "O'lcham", 'Rang'])
    for o in ctx['out_of_stock']:
        w.writerow([o.branch.name, o.variant.product.code, o.variant.size, o.variant.color])
    return response


def _insights_pdf(ctx):
    response = HttpResponse(content_type='application/pdf')
    fn = f"Biznes_tahlili_{ctx['d_start']}_{ctx['d_end']}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{fn}"'
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
        title="Biznes tahlili",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('H1', parent=styles['Title'], fontSize=18, spaceAfter=2, alignment=0)
    h2 = ParagraphStyle('H2', parent=styles['Heading3'], fontSize=12,
                        spaceBefore=8, spaceAfter=4, textColor=colors.HexColor('#0d6efd'))
    meta = ParagraphStyle('M', parent=styles['Normal'], fontSize=9,
                          textColor=colors.HexColor('#666'))

    branch_str = ctx['selected_branch'].name if ctx['selected_branch'] else 'Barcha filiallar'
    elements = [
        Paragraph("Srote — Biznes tahlili", h1),
        Paragraph(f"Davr: <b>{ctx['d_start']} — {ctx['d_end']}</b> ({ctx['days']} kun) &nbsp; | &nbsp; "
                  f"Filial: <b>{branch_str}</b> &nbsp; | &nbsp; "
                  f"Yaratildi: {timezone.localtime().strftime('%Y-%m-%d %H:%M')}", meta),
        Spacer(1, 4*mm),
    ]

    def header_style(t):
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbb')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1),
             [colors.white, colors.HexColor('#f4f6fa')]),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        return t

    # KPI table
    elements.append(Paragraph("Asosiy ko'rsatkichlar", h2))
    kpi = [
        ['Ko\'rsatkich', 'Qiymat', 'Oldingi davr', "O'sish %"],
        ["Daromad (so'm)", _fmt(ctx['revenue']), _fmt(ctx['prev_revenue']),
         f"{ctx['rev_growth']:+.1f}%" if ctx['rev_growth'] is not None else '—'],
        ["Tannarx (so'm)", _fmt(ctx['total_cost']), '—', '—'],
        ["Sof foyda (so'm)", _fmt(ctx['profit']), '—', '—'],
        ['Foyda marjasi (%)', f"{ctx['margin']:.2f}%", '—', '—'],
        ['Sotuvlar', _fmt(ctx['sales_count']), _fmt(ctx['prev_n']),
         f"{ctx['sales_growth']:+.1f}%" if ctx['sales_growth'] is not None else '—'],
        ['Dona', _fmt(ctx['qty']), _fmt(ctx['prev_qty']),
         f"{ctx['qty_growth']:+.1f}%" if ctx['qty_growth'] is not None else '—'],
        ["O'rtacha sotuv (so'm)", _fmt(ctx['avg_sale_value']), '—', '—'],
    ]
    elements.append(header_style(Table(kpi, repeatRows=1,
                                       colWidths=[55*mm, 40*mm, 40*mm, 35*mm])))

    # Filial taqqoslash
    if ctx['branch_compare']:
        elements.append(Paragraph("Filiallar taqqoslash", h2))
        data = [['Filial', "Daromad", "Foyda", 'Marja', 'Sotuvlar', 'Dona', 'Omborda']]
        for b in ctx['branch_compare']:
            data.append([
                b['branch'].name, _fmt(b['revenue']), _fmt(b['profit']),
                f"{b['margin']:.1f}%", _fmt(b['sales_count']),
                _fmt(b['qty']), _fmt(b['stock']),
            ])
        elements.append(header_style(Table(data, repeatRows=1)))

    # Top by revenue
    elements.append(Paragraph("TOP-10 mahsulot (daromad bo'yicha)", h2))
    data = [['#', 'Kod', 'Mahsulot', 'Dona', 'Sotuvlar', "Daromad"]]
    for i, p in enumerate(ctx['top_products'], 1):
        data.append([str(i), p['variant__product__code'],
                     p['variant__product__name'][:40],
                     _fmt(p['qty']), _fmt(p['n_sales']), _fmt(p['revenue'])])
    elements.append(header_style(Table(data, repeatRows=1)))

    # Top by profit
    elements.append(Paragraph("TOP-10 mahsulot (sof foyda bo'yicha)", h2))
    data = [['#', 'Kod', 'Mahsulot', "Daromad", "Foyda", 'Marja']]
    for i, p in enumerate(ctx['top_profit'], 1):
        data.append([str(i), p['code'], p['name'][:40],
                     _fmt(p['revenue']), _fmt(p['profit']),
                     f"{p['margin']:.1f}%"])
    elements.append(header_style(Table(data, repeatRows=1)))

    # Slow movers
    if ctx['slow_products']:
        elements.append(Paragraph("Sotilmayotgan mahsulotlar", h2))
        data = [['Kod', 'Mahsulot', 'Kategoriya', 'Omborda']]
        for p in ctx['slow_products']:
            data.append([p.code, p.name[:40],
                         p.category.name if p.category else '—',
                         _fmt(p.stock or 0)])
        elements.append(header_style(Table(data, repeatRows=1)))

    # Top sellers
    elements.append(Paragraph("Top-10 sotuvchilar", h2))
    data = [['#', 'Username', 'Filial', 'Sotuvlar', 'Dona', "Daromad"]]
    for i, s in enumerate(ctx['top_sellers'], 1):
        data.append([str(i), s['sold_by__username'],
                     s['sold_by__branch__name'] or '—',
                     _fmt(s['n_sales']), _fmt(s['qty']), _fmt(s['revenue'])])
    elements.append(header_style(Table(data, repeatRows=1)))

    # Turnover
    if ctx['turnover']:
        elements.append(Paragraph("Tovar aylanmasi", h2))
        data = [['Kod', 'Mahsulot', 'Kunlik o\'rtacha', 'Omborda', 'Necha kun yetadi']]
        for t in ctx['turnover']:
            dleft = f"{t['days_left']:.0f} kun" if t['days_left'] is not None else '—'
            data.append([t['code'], t['name'][:40],
                         f"{t['daily_avg']:.2f}", _fmt(t['stock']), dleft])
        elements.append(header_style(Table(data, repeatRows=1)))

    def _footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#999'))
        canvas.drawRightString(doc_obj.pagesize[0] - 15*mm, 8*mm,
                               f"Srote — bet {doc_obj.page}")
        canvas.restoreState()

    doc.build(elements, onFirstPage=_footer, onLaterPages=_footer)
    response.write(buf.getvalue())
    buf.close()
    return response


def _csv_response(title, headers, rows, summary, d_start, d_end, branch):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    fn = f"{title.replace(' ', '_')}_{d_start}_{d_end}.csv"
    response['Content-Disposition'] = f'attachment; filename="{fn}"'
    response.write('﻿')  # BOM for Excel
    writer = csv.writer(response)
    writer.writerow([title])
    writer.writerow([f"Davr: {d_start} — {d_end}"])
    if branch:
        writer.writerow([f"Filial: {branch.name}"])
    else:
        writer.writerow(["Filial: Barchasi"])
    writer.writerow([])
    writer.writerow(headers)
    for r in rows:
        writer.writerow(r)
    writer.writerow([])
    writer.writerow(['Xulosa:'])
    for k, v in summary.items():
        writer.writerow([k, v])
    return response


def _fmt(v):
    """PDF jadval uchun qiymatni formatlash."""
    if v is None:
        return ''
    if hasattr(v, 'quantize'):  # Decimal
        return f"{v:,.0f}".replace(',', ' ')
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:,}".replace(',', ' ')
    return str(v)


def _pdf_response(title, headers, rows, summary, d_start, d_end, branch):
    response = HttpResponse(content_type='application/pdf')
    fn = f"{title.replace(' ', '_').replace(chr(39), '')}_{d_start}_{d_end}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{fn}"'

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=12*mm, rightMargin=12*mm,
        topMargin=12*mm, bottomMargin=12*mm,
        title=title,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'TitleU', parent=styles['Title'], fontSize=16, spaceAfter=4, alignment=0,
    )
    meta_style = ParagraphStyle(
        'MetaU', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#555'),
    )

    elements = []
    elements.append(Paragraph(title, title_style))
    branch_str = branch.name if branch else "Barcha filiallar"
    elements.append(Paragraph(
        f"Davr: <b>{d_start} — {d_end}</b> &nbsp; | &nbsp; "
        f"Filial: <b>{branch_str}</b> &nbsp; | &nbsp; "
        f"Yaratildi: {timezone.localtime().strftime('%Y-%m-%d %H:%M')}",
        meta_style,
    ))
    elements.append(Spacer(1, 6*mm))

    data = [list(headers)]
    for row in rows:
        data.append([_fmt(c) for c in row])
    if len(data) == 1:
        data.append(['—'] + [''] * (len(headers) - 1))

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbbbbb')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
         [colors.white, colors.HexColor('#f4f6fa')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(table)

    if summary:
        elements.append(Spacer(1, 8*mm))
        elements.append(Paragraph("<b>Xulosa:</b>", styles['Heading4']))
        summary_data = [[k, _fmt(v)] for k, v in summary.items()]
        st = Table(summary_data, colWidths=[100*mm, 60*mm])
        st.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbbbbb')),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f4f6fa')),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(st)

    def _page_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#999'))
        canvas.drawRightString(
            doc_obj.pagesize[0] - 12*mm, 8*mm,
            f"Srote Ombor Boshqaruv  -  bet {doc_obj.page}"
        )
        canvas.restoreState()

    doc.build(elements, onFirstPage=_page_footer, onLaterPages=_page_footer)
    response.write(buf.getvalue())
    buf.close()
    return response
