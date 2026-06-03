from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, F, Q, DecimalField, ExpressionWrapper, Count
from django.db.models.functions import Coalesce
from django.db import transaction
from django.http import HttpResponseForbidden, HttpResponse, JsonResponse
import json as _json
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
    Category, Intake, Sale, AuditLog, SaleTransaction, Return, Customer, Shift,
    Transfer, TransferLine, Stocktake, StocktakeCount,
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


# ---------- HEALTH ----------

def healthz(request):
    """Liveness probe for Render / monitoring. Cheap, no DB hit."""
    return HttpResponse('ok', content_type='text/plain')


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
    raw_query = (request.GET.get('code') or '').strip()
    code = normalize_code(raw_query.upper())
    product = None
    branches_data = []
    suggestions = []

    if raw_query:
        # Try exact code first
        product = Product.objects.filter(code=code).first()

        # If no code match, fall back to name / category / description search
        if not product:
            matches = (Product.objects
                .filter(Q(name__icontains=raw_query) |
                        Q(category__name__icontains=raw_query) |
                        Q(description__icontains=raw_query))
                .select_related('category')[:20])
            if matches.count() == 1:
                product = matches.first()
                code = product.code
            elif matches.exists():
                suggestions = list(matches)
            else:
                messages.warning(request,
                    f"'{raw_query}' bo'yicha mahsulot topilmadi.")

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

    return render(request, 'inventory/lookup.html', {
        'code': raw_query, 'product': product,
        'branches_data': branches_data, 'suggestions': suggestions,
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

def _split_csv(text):
    """Split a comma- or newline-separated string into trimmed tokens."""
    if not text:
        return []
    parts = []
    for chunk in text.replace('\n', ',').split(','):
        c = chunk.strip()
        if c and c not in parts:
            parts.append(c)
    return parts


@admin_required
def intake_for_product(request, code):
    """Bulk intake: matrix of (size × color) → counts in one form."""
    from decimal import Decimal
    product = get_object_or_404(Product, code=normalize_code(code))

    if request.method == 'POST':
        try:
            branch_id = int(request.POST.get('branch') or 0)
            branch = Branch.objects.filter(pk=branch_id, is_active=True).first()
            if not branch:
                messages.error(request, "Filial tanlang.")
                return redirect('intake_for_product', code=product.code)

            cost = Decimal(request.POST.get('cost_per_unit') or '0')
            markup = Decimal(request.POST.get('markup_percent') or '0')
            sale_price_raw = request.POST.get('sale_price') or ''
            if sale_price_raw.strip():
                sale_price = Decimal(sale_price_raw)
            else:
                sale_price = (cost * (1 + markup / 100)).quantize(Decimal('1'))

            supplier = (request.POST.get('supplier') or '').strip()
            note = (request.POST.get('note') or '').strip()
            update_price = bool(request.POST.get('update_product_price'))

            # Iterate matrix cells. Inputs are named qty[<size>|<color>].
            total_qty = 0
            variants_touched = 0
            with transaction.atomic():
                for key, value in request.POST.items():
                    if not key.startswith('qty[') or not key.endswith(']'):
                        continue
                    payload = key[4:-1]
                    if '|' not in payload:
                        continue
                    size, color = payload.split('|', 1)
                    size = size.strip()
                    color = color.strip()
                    if not size or not color:
                        continue
                    try:
                        qty = int(value)
                    except (ValueError, TypeError):
                        continue
                    if qty <= 0:
                        continue
                    variant, _ = ProductVariant.objects.get_or_create(
                        product=product, size=size, color=color,
                    )
                    stock, _ = BranchStock.objects.get_or_create(
                        variant=variant, branch=branch,
                        defaults={'cost_price': cost, 'sale_price': sale_price},
                    )
                    stock.stock_count = F('stock_count') + qty
                    stock.cost_price = cost
                    stock.sale_price = sale_price
                    stock.save()
                    Intake.objects.create(
                        variant=variant, branch=branch,
                        quantity=qty, cost_per_unit=cost,
                        supplier=supplier, note=note,
                        received_by=request.user,
                    )
                    total_qty += qty
                    variants_touched += 1

                if update_price and sale_price > 0:
                    product.default_sale_price = sale_price
                    product.markup_percent = markup
                    product.save()
                    BranchStock.objects.filter(
                        variant__product=product, sale_price=0
                    ).update(sale_price=sale_price)

            if variants_touched == 0:
                messages.warning(request,
                    "Hech qaysi katakka son kiritilmadi — saqlanmadi.")
            else:
                messages.success(request,
                    f"Qabul saqlandi: {branch.name}ga {variants_touched} ta "
                    f"variant uchun jami {total_qty} dona.")
                return redirect('product_detail', code=product.code)
        except (ValueError, TypeError) as e:
            messages.error(request, f"Maydonlarni tekshiring: {e}")

    # Pre-fill sizes/colors from existing variants if present
    variants = list(product.variants.all())
    existing_sizes = sorted({v.size for v in variants}, key=lambda s: (len(s), s))
    existing_colors = sorted({v.color for v in variants})

    return render(request, 'inventory/intake_form.html', {
        'product': product,
        'branches': Branch.objects.filter(is_active=True),
        'existing_sizes': existing_sizes,
        'existing_colors': existing_colors,
    })


@admin_required
def intake_new(request):
    products = Product.objects.order_by('-created_at')[:50]
    return render(request, 'inventory/intake_choose.html', {'products': products})


# ---------- STOCKTAKE (physical count vs system) ----------

@admin_required
def stocktake_list(request):
    sessions = (Stocktake.objects.select_related('branch', 'started_by', 'applied_by')
                .order_by('-started_at')[:50])
    return render(request, 'inventory/stocktake_list.html', {'sessions': sessions})


@admin_required
def stocktake_create(request):
    branches = Branch.objects.filter(is_active=True)
    if request.method == 'POST':
        try:
            branch_id = int(request.POST.get('branch') or 0)
        except ValueError:
            branch_id = 0
        branch = Branch.objects.filter(pk=branch_id, is_active=True).first()
        if not branch:
            messages.error(request, "Filial tanlang.")
            return redirect('stocktake_create')
        # Snapshot every variant with stock>0 in this branch
        with transaction.atomic():
            session = Stocktake.objects.create(
                branch=branch, started_by=request.user,
                note=(request.POST.get('note') or '').strip()[:200],
            )
            stocks = BranchStock.objects.filter(branch=branch).select_related('variant')
            counts = [
                StocktakeCount(session=session, variant=s.variant,
                               system_qty=s.stock_count, counted_qty=s.stock_count)
                for s in stocks
            ]
            StocktakeCount.objects.bulk_create(counts)
        messages.success(request, f"Inventarizatsiya boshlandi: {len(counts)} ta variant.")
        return redirect('stocktake_detail', pk=session.pk)
    return render(request, 'inventory/stocktake_create.html', {'branches': branches})


@admin_required
def stocktake_detail(request, pk):
    session = get_object_or_404(
        Stocktake.objects.select_related('branch', 'started_by', 'applied_by'),
        pk=pk,
    )
    counts = (session.counts.select_related('variant__product')
              .order_by('variant__product__code', 'variant__size'))

    if request.method == 'POST' and session.status == Stocktake.Status.OPEN:
        action = request.POST.get('action')
        if action == 'save':
            # Update counted_qty for each row
            with transaction.atomic():
                for c in counts:
                    val = request.POST.get(f'count[{c.pk}]')
                    if val is None or val == '':
                        continue
                    try:
                        c.counted_qty = max(0, int(val))
                        c.save(update_fields=['counted_qty'])
                    except ValueError:
                        pass
            messages.success(request, "Hisoblangan miqdorlar saqlandi.")
            return redirect('stocktake_detail', pk=session.pk)

        if action == 'apply':
            # Apply: adjust BranchStock to match counted_qty
            with transaction.atomic():
                for c in counts:
                    bs = BranchStock.objects.filter(
                        variant=c.variant, branch=session.branch
                    ).first()
                    if bs:
                        bs.stock_count = c.counted_qty
                        bs.save(update_fields=['stock_count'])
                session.status = Stocktake.Status.APPLIED
                session.applied_by = request.user
                session.applied_at = timezone.now()
                session.save()
            messages.success(request, "Inventarizatsiya tasdiqlandi va ombor yangilandi.")
            return redirect('stocktake_detail', pk=session.pk)

    diffs = sum(1 for c in counts if c.diff != 0)
    return render(request, 'inventory/stocktake_detail.html', {
        'session': session, 'counts': counts, 'diff_count': diffs,
    })


# ---------- TRANSFERS (inter-branch stock moves) ----------

@admin_required
def transfer_list(request):
    transfers = (Transfer.objects.select_related('from_branch', 'to_branch',
                                                  'created_by', 'received_by')
                 .prefetch_related('lines')
                 .order_by('-created_at')[:100])
    return render(request, 'inventory/transfer_list.html', {'transfers': transfers})


@admin_required
def transfer_create(request):
    branches = Branch.objects.filter(is_active=True)
    if request.method == 'POST':
        try:
            from_id = int(request.POST.get('from_branch') or 0)
            to_id = int(request.POST.get('to_branch') or 0)
        except ValueError:
            messages.error(request, "Filial tanlanmagan."); return redirect('transfer_create')
        if from_id == to_id:
            messages.error(request, "Bir xil filialga ko'chirib bo'lmaydi.")
            return redirect('transfer_create')
        from_branch = Branch.objects.filter(pk=from_id).first()
        to_branch = Branch.objects.filter(pk=to_id).first()
        if not from_branch or not to_branch:
            messages.error(request, "Filiallar topilmadi."); return redirect('transfer_create')

        # Parse line items: qty[<variant_id>]
        lines_data = []
        for key, value in request.POST.items():
            if not key.startswith('qty[') or not key.endswith(']'):
                continue
            try:
                variant_id = int(key[4:-1])
                qty = int(value)
            except ValueError:
                continue
            if qty <= 0:
                continue
            lines_data.append((variant_id, qty))

        if not lines_data:
            messages.error(request, "Birorta tovar miqdori kiritilmagan.")
            return redirect('transfer_create')

        # Validate enough stock in source branch
        problems = []
        for variant_id, qty in lines_data:
            stock = BranchStock.objects.filter(variant_id=variant_id, branch=from_branch).first()
            available = stock.stock_count if stock else 0
            if qty > available:
                v = ProductVariant.objects.get(pk=variant_id)
                problems.append(f"{v.product.code} {v.size}/{v.color}: {from_branch.name}da {available} bor, so'rov {qty}")
        if problems:
            for p in problems:
                messages.error(request, p)
            return redirect('transfer_create')

        with transaction.atomic():
            t = Transfer.objects.create(
                from_branch=from_branch, to_branch=to_branch,
                created_by=request.user,
                status=Transfer.Status.IN_TRANSIT,
                dispatched_at=timezone.now(),
                note=(request.POST.get('note') or '').strip()[:200],
            )
            for variant_id, qty in lines_data:
                TransferLine.objects.create(transfer=t, variant_id=variant_id, quantity=qty)
                # Decrement source branch immediately on dispatch
                stock = BranchStock.objects.filter(
                    variant_id=variant_id, branch=from_branch).first()
                stock.stock_count = F('stock_count') - qty
                stock.save()

        messages.success(request, f"Ko'chirish #{t.pk} yo'lga chiqarildi.")
        return redirect('transfer_detail', pk=t.pk)

    return render(request, 'inventory/transfer_create.html', {'branches': branches})


@admin_required
def transfer_detail(request, pk):
    t = get_object_or_404(
        Transfer.objects.select_related('from_branch', 'to_branch',
                                         'created_by', 'received_by')
                         .prefetch_related('lines__variant__product'),
        pk=pk,
    )
    return render(request, 'inventory/transfer_detail.html', {'transfer': t})


@admin_required
def transfer_receive(request, pk):
    t = get_object_or_404(Transfer, pk=pk)
    if t.status != Transfer.Status.IN_TRANSIT:
        messages.warning(request, "Bu ko'chirish allaqachon yopilgan.")
        return redirect('transfer_detail', pk=t.pk)

    if request.method == 'POST':
        with transaction.atomic():
            for line in t.lines.all():
                stock, _ = BranchStock.objects.get_or_create(
                    variant=line.variant, branch=t.to_branch,
                    defaults={'cost_price': 0, 'sale_price': 0},
                )
                stock.stock_count = F('stock_count') + line.quantity
                stock.save()
            t.status = Transfer.Status.RECEIVED
            t.received_by = request.user
            t.received_at = timezone.now()
            t.save()
        messages.success(request, f"Ko'chirish #{t.pk} qabul qilindi.")
        return redirect('transfer_detail', pk=t.pk)

    return render(request, 'inventory/transfer_receive.html', {'transfer': t})


# ---------- SHIFTS ----------

def _open_shift_for(branch):
    """Returns the single open shift for a branch, or None."""
    return Shift.objects.filter(branch=branch, status=Shift.Status.OPEN).first()


@login_required
def shift_open(request):
    branch = _user_branch_or_403(request)
    if branch is None:
        messages.error(request, "Filial biriktirilmagan.")
        return redirect('lookup')

    existing = _open_shift_for(branch)
    if existing:
        messages.info(request, f"Ochiq smen allaqachon bor (#{existing.pk}).")
        return redirect('pos_terminal')

    if request.method == 'POST':
        try:
            opening_cash = max(0, float(request.POST.get('opening_cash') or 0))
        except ValueError:
            opening_cash = 0
        Shift.objects.create(
            branch=branch, opened_by=request.user,
            opening_cash=opening_cash,
            note=(request.POST.get('note') or '').strip()[:200],
        )
        messages.success(request, f"Smen ochildi (boshlang'ich naqd: {opening_cash:,.0f} so'm).")
        return redirect('pos_terminal')

    return render(request, 'inventory/shift_open.html', {'branch': branch})


@login_required
def shift_close(request):
    branch = _user_branch_or_403(request)
    if branch is None:
        return redirect('lookup')

    shift = _open_shift_for(branch)
    if not shift:
        messages.warning(request, "Ochiq smen yo'q.")
        return redirect('pos_terminal')

    if request.method == 'POST':
        try:
            counted = max(0, float(request.POST.get('counted_cash') or 0))
        except ValueError:
            counted = 0
        with transaction.atomic():
            shift.counted_cash = counted
            shift.closed_by = request.user
            shift.closed_at = timezone.now()
            shift.status = Shift.Status.CLOSED
            shift.note = (
                (shift.note + ' | ' if shift.note else '')
                + (request.POST.get('note') or '').strip()
            )[:200]
            shift.save()
        var = shift.variance()
        if var is not None and abs(var) >= 1:
            messages.warning(request,
                f"Smen yopildi. Kassa farqi: {var:+,.0f} so'm "
                f"({'ortiq' if var > 0 else 'kam'}).")
        else:
            messages.success(request, "Smen yopildi. Kassa to'g'ri.")
        return redirect('shift_detail', pk=shift.pk)

    return render(request, 'inventory/shift_close.html', {
        'shift': shift,
        'expected': shift.expected_cash(),
        'cash_sales': shift.cash_sales(),
    })


@login_required
def shift_detail(request, pk):
    shift = get_object_or_404(Shift, pk=pk)
    if not request.user.is_admin() and request.user.branch_id != shift.branch_id:
        return HttpResponseForbidden()
    txns = (shift.transactions.select_related('sold_by')
            .prefetch_related('lines')
            .order_by('-sold_at')[:200])
    return render(request, 'inventory/shift_detail.html', {
        'shift': shift,
        'txns': txns,
        'cash_sales': shift.cash_sales(),
        'expected': shift.expected_cash(),
    })


@admin_required
def shift_list(request):
    shifts = (Shift.objects.select_related('branch', 'opened_by', 'closed_by')
              .order_by('-opened_at')[:100])
    return render(request, 'inventory/shift_list.html', {'shifts': shifts})


# ---------- POS TERMINAL ----------

def _user_branch_or_403(request):
    """Sellers must have a branch. Admins use (in order):
       1) their assigned branch, 2) the branch of an open shift they
       themselves opened, 3) any open shift, 4) first active branch.
    """
    if request.user.is_admin():
        if request.user.branch_id:
            return request.user.branch
        own_shift = Shift.objects.filter(
            opened_by=request.user, status=Shift.Status.OPEN
        ).select_related('branch').order_by('-opened_at').first()
        if own_shift:
            return own_shift.branch
        any_shift = Shift.objects.filter(
            status=Shift.Status.OPEN
        ).select_related('branch').order_by('-opened_at').first()
        if any_shift:
            return any_shift.branch
        first = Branch.objects.filter(is_active=True).first()
        if first:
            return first
        return None
    return request.user.branch


@login_required
def pos_terminal(request):
    """Single-page POS UI. Browser maintains the cart, posts via AJAX."""
    branch = _user_branch_or_403(request)
    if branch is None:
        messages.error(request, "Filial biriktirilmagan. Administrator bilan bog'laning.")
        return redirect('lookup')

    open_shift = _open_shift_for(branch)
    if not open_shift:
        return redirect('shift_open')

    recent_txns = (SaleTransaction.objects.filter(branch=branch, shift=open_shift)
                   .select_related('sold_by')
                   .prefetch_related('lines')
                   .order_by('-sold_at')[:5])

    return render(request, 'inventory/pos.html', {
        'branch': branch,
        'shift': open_shift,
        'recent_txns': recent_txns,
        'payment_methods': SaleTransaction.PaymentMethod.choices,
    })


@login_required
def pos_lookup(request):
    """GET /pos/lookup/?q=OYO-0001 [&branch=ID]
    Admins may pass &branch= to query a different branch (used by transfers).
    Returns JSON:
      { found: bool,
        product: {code, name, default_sale_price},
        variants: [{variant_id, stock_id, size, color, stock_count, sale_price, cost_price}],
        suggestions: [{code, name}] }
    """
    # Admins may override the branch context (e.g. transfer create form)
    branch_override = request.GET.get('branch')
    if branch_override and request.user.is_admin():
        try:
            branch = Branch.objects.get(pk=int(branch_override))
        except (Branch.DoesNotExist, ValueError):
            branch = _user_branch_or_403(request)
    else:
        branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'error': 'no branch'}, status=403)

    q = (request.GET.get('q') or '').strip()
    if not q:
        return JsonResponse({'found': False})

    # Exact code first
    code = normalize_code(q.upper())
    product = Product.objects.filter(code=code).first()

    if not product:
        # Name search
        matches = list(Product.objects.filter(
            Q(name__icontains=q) | Q(category__name__icontains=q)
        )[:8])
        if len(matches) == 1:
            product = matches[0]
        elif matches:
            return JsonResponse({
                'found': False,
                'suggestions': [{'code': p.code, 'name': p.name} for p in matches],
            })
        else:
            return JsonResponse({'found': False, 'suggestions': []})

    stocks = (BranchStock.objects.filter(variant__product=product, branch=branch)
              .select_related('variant')
              .order_by('variant__size', 'variant__color'))

    variants = [{
        'stock_id': s.id,
        'variant_id': s.variant_id,
        'size': s.variant.size,
        'color': s.variant.color,
        'stock_count': s.stock_count,
        'sale_price': float(s.sale_price or product.default_sale_price),
        'cost_price': float(s.cost_price),
    } for s in stocks]

    # If the product exists but has no variants in this branch, tell the
    # user explicitly — the silent "topilmadi" is misleading.
    other_branches_with_stock = []
    if not variants:
        other_ids = (BranchStock.objects
                     .filter(variant__product=product, stock_count__gt=0)
                     .exclude(branch=branch)
                     .values_list('branch_id', flat=True).distinct())
        for b in Branch.objects.filter(pk__in=other_ids):
            other_branches_with_stock.append({'name': b.name, 'id': b.id})

    return JsonResponse({
        'found': True,
        'product': {
            'code': product.code,
            'name': product.name,
            'default_sale_price': float(product.default_sale_price),
        },
        'branch_name': branch.name,
        'variants': variants,
        'other_branches': other_branches_with_stock,
    })


@login_required
def pos_checkout(request):
    """POST /pos/checkout/ with JSON body:
      { lines: [{stock_id, qty, sale_price}],
        payment_method: 'cash'|'card'|'transfer'|'mixed',
        customer_name, customer_phone, note }
    Creates SaleTransaction + Sales atomically. Returns {ok, txn_id, receipt_url}.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)

    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)

    lines = data.get('lines') or []
    if not lines:
        return JsonResponse({'ok': False, 'error': 'savat boʻsh'}, status=400)

    payment_method = (data.get('payment_method') or 'cash').strip()
    customer_name = (data.get('customer_name') or '').strip()[:120]
    customer_phone = (data.get('customer_phone') or '').strip()[:40]
    note = (data.get('note') or '').strip()[:200]
    try:
        order_discount = max(0, float(data.get('order_discount') or 0))
    except (ValueError, TypeError):
        order_discount = 0
    discount_reason = (data.get('discount_reason') or '').strip()[:200]

    # Validate stock + collect resolved BranchStock objects
    resolved = []
    for ln in lines:
        try:
            sid = int(ln['stock_id'])
            qty = int(ln['qty'])
            line_discount = max(0, float(ln.get('line_discount') or 0))
            price = float(ln['sale_price'])
        except (KeyError, ValueError, TypeError):
            return JsonResponse({'ok': False, 'error': 'noto\'g\'ri qator'}, status=400)
        if qty <= 0 or price < 0:
            return JsonResponse({'ok': False, 'error': 'qty/narx noto\'g\'ri'}, status=400)
        stock = BranchStock.objects.select_related('variant__product', 'branch') \
            .filter(pk=sid, branch=branch).first()
        if not stock:
            return JsonResponse({'ok': False, 'error': f'stock {sid} topilmadi'}, status=400)
        if qty > stock.stock_count:
            return JsonResponse({
                'ok': False,
                'error': f"{stock.variant.product.code} {stock.variant.size}/{stock.variant.color}: "
                         f"omborda faqat {stock.stock_count} ta bor, soʻrov {qty}",
            }, status=400)
        resolved.append((stock, qty, price, line_discount))

    # Resolve / auto-create Customer by phone (most reliable key)
    customer = None
    if customer_phone:
        cleaned_phone = ''.join(c for c in customer_phone if c.isdigit() or c == '+')
        if cleaned_phone:
            customer = Customer.objects.filter(phone=cleaned_phone).first()
            if not customer:
                customer = Customer.objects.create(
                    phone=cleaned_phone, name=customer_name,
                )
            elif customer_name and not customer.name:
                customer.name = customer_name
                customer.save(update_fields=['name'])

    open_shift = _open_shift_for(branch)

    with transaction.atomic():
        txn = SaleTransaction.objects.create(
            branch=branch,
            sold_by=request.user,
            payment_method=payment_method,
            customer=customer,
            customer_name=customer_name,
            customer_phone=customer_phone,
            note=note,
            order_discount=order_discount,
            discount_reason=discount_reason,
            shift=open_shift,
        )
        for stock, qty, price, ld in resolved:
            stock.stock_count = F('stock_count') - qty
            stock.save()
            Sale.objects.create(
                transaction=txn,
                variant=stock.variant, branch=stock.branch,
                quantity=qty,
                sale_price=price,
                cost_at_sale=stock.cost_price,
                line_discount=ld,
                sold_by=request.user,
            )

    # Best-effort fiscal (noop unless provider configured)
    from .fiscal import submit_for_transaction
    submit_for_transaction(txn)

    return JsonResponse({
        'ok': True,
        'txn_id': txn.pk,
        'receipt_url': f'/transaction/{txn.pk}/?autoprint=1',
        'total': float(txn.total),
        'item_count': txn.item_count,
    })


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
                # Wrap every sale in a SaleTransaction so the OFD/fiscal
                # submission flow is the same for single-item and cart sales.
                txn = SaleTransaction.objects.create(
                    branch=stock.branch,
                    sold_by=request.user,
                    payment_method=SaleTransaction.PaymentMethod.CASH,
                    note=cd.get('note') or '',
                )
                Sale.objects.create(
                    transaction=txn,
                    variant=stock.variant, branch=stock.branch,
                    quantity=qty, sale_price=cd['sale_price'],
                    cost_at_sale=stock.cost_price,
                    note=cd.get('note') or '', sold_by=request.user,
                )

            # Best-effort fiscal submission (no-op when no OFD configured).
            from .fiscal import submit_for_transaction
            submit_for_transaction(txn)

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

        from .fiscal import submit_for_transaction
        submit_for_transaction(txn)

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


# ---------- CUSTOMERS ----------

@admin_required
def customer_list(request):
    q = (request.GET.get('q') or '').strip()
    customers = Customer.objects.all()
    if q:
        customers = customers.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    # annotate quick stats
    revenue_expr = ExpressionWrapper(
        F('transactions__lines__quantity') * F('transactions__lines__sale_price'),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    customers = customers.annotate(
        txn_count=Count('transactions', distinct=True),
        total_spent=Coalesce(Sum(revenue_expr), 0,
                             output_field=DecimalField(max_digits=14, decimal_places=2)),
    ).order_by('-total_spent', 'name')[:200]
    return render(request, 'inventory/customer_list.html', {
        'customers': customers, 'q': q,
    })


@admin_required
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    txns = (customer.transactions.select_related('branch', 'sold_by')
            .prefetch_related('lines__variant__product')
            .order_by('-sold_at')[:50])
    revenue_expr = ExpressionWrapper(
        F('lines__quantity') * F('lines__sale_price'),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    stats = customer.transactions.aggregate(
        n=Count('id', distinct=True),
        total=Coalesce(Sum(revenue_expr), 0,
                       output_field=DecimalField(max_digits=14, decimal_places=2)),
    )
    return render(request, 'inventory/customer_detail.html', {
        'customer': customer, 'txns': txns,
        'stats': stats,
    })


@login_required
def pos_customer_lookup(request):
    """JSON: GET /pos/customer/?phone=998... → returns matches."""
    phone = (request.GET.get('phone') or '').strip()
    if len(phone) < 3:
        return JsonResponse({'matches': []})
    cleaned = ''.join(c for c in phone if c.isdigit() or c == '+')
    if not cleaned:
        return JsonResponse({'matches': []})
    qs = Customer.objects.filter(phone__icontains=cleaned)[:5]
    return JsonResponse({'matches': [
        {'id': c.pk, 'name': c.name, 'phone': c.phone} for c in qs
    ]})


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

    # Net revenue = qty*price - line_discount. Whole-order discount is
    # attached to SaleTransaction so we subtract it at the txn-id aggregate.
    revenue_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )

    totals = sales.aggregate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
        sales_count=Count('id'),
    )

    # Subtract whole-order discounts (stored on SaleTransaction)
    txn_qs = SaleTransaction.objects.filter(
        sold_at__gte=dt_start, sold_at__lt=dt_end
    )
    if branch_id and selected_branch:
        txn_qs = txn_qs.filter(branch=selected_branch)
    order_disc = txn_qs.aggregate(s=Sum('order_discount'))['s'] or 0

    revenue = (totals['revenue'] or 0) - order_disc
    qty = totals['qty'] or 0
    sales_count = totals['sales_count'] or 0
    discount_total = order_disc + (sales.aggregate(s=Sum('line_discount'))['s'] or 0)

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
        'discount_total': discount_total,
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
