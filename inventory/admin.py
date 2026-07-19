from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    User, Branch, Category, Product, ProductVariant,
    BranchStock, Intake, Sale, AuditLog, SaleTransaction, Return, Customer,
    ParkedSale, Promotion, PaymentQR, PaymentIntent,
    Supplier, IntakeSession, ProductRequest, CashPayout, InvoiceDraft,
    InvoiceImage,
)


@admin.register(InvoiceImage)
class InvoiceImageAdmin(admin.ModelAdmin):
    list_display = ('id', 'order', 'draft', 'session', 'created_at')
    list_filter = ('created_at',)


@admin.register(InvoiceDraft)
class InvoiceDraftAdmin(admin.ModelAdmin):
    list_display = ('id', 'supplier_text', 'invoice_number', 'branch',
                    'row_count', 'total_qty', 'created_by', 'updated_at')
    list_filter = ('branch',)
    search_fields = ('supplier_text', 'invoice_number')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ProductRequest)
class ProductRequestAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'customer_phone', 'branch',
                    'requested_by', 'created_at')
    list_filter = ('status', 'branch')
    search_fields = ('name', 'customer_phone', 'note')
    readonly_fields = ('created_at',)


@admin.register(CashPayout)
class CashPayoutAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'amount', 'category', 'branch',
                    'shift', 'created_by', 'note')
    list_filter = ('category', 'branch')
    search_fields = ('note',)
    readonly_fields = ('created_at',)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'contact_person', 'inn', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'phone', 'inn', 'contact_person')


@admin.register(IntakeSession)
class IntakeSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'branch', 'supplier_display', 'received_by',
                    'invoice_number', 'received_at')
    list_filter = ('branch', 'supplier')
    search_fields = ('invoice_number', 'supplier_text', 'note')
    readonly_fields = ('received_at',)


@admin.register(PaymentIntent)
class PaymentIntentAdmin(admin.ModelAdmin):
    list_display = ('id', 'branch', 'provider', 'amount', 'ref_code',
                    'status', 'initiated_by', 'created_at', 'paid_at')
    list_filter = ('status', 'provider', 'branch')
    search_fields = ('ref_code', 'provider_txn_id')
    readonly_fields = ('created_at', 'paid_at', 'cart_snapshot')
    actions = ['mark_paid', 'mark_cancelled']

    def mark_paid(self, request, queryset):
        from django.utils import timezone
        n = queryset.filter(status='pending').update(
            status='paid', paid_at=timezone.now()
        )
        self.message_user(request, f"{n} ta intent paid deb belgilandi.")
    mark_paid.short_description = "Tanlanganlarni 'To'landi' deb belgilash (test)"

    def mark_cancelled(self, request, queryset):
        n = queryset.filter(status='pending').update(status='cancelled')
        self.message_user(request, f"{n} ta intent bekor qilindi.")
    mark_cancelled.short_description = "Tanlanganlarni 'Bekor' deb belgilash"


@admin.register(PaymentQR)
class PaymentQRAdmin(admin.ModelAdmin):
    list_display = ('branch', 'provider', 'label', 'is_active', 'created_at')
    list_filter = ('branch', 'provider', 'is_active')
    search_fields = ('label',)


@admin.register(ParkedSale)
class ParkedSaleAdmin(admin.ModelAdmin):
    list_display = ('id', 'label', 'branch', 'parked_by', 'created_at')
    list_filter = ('branch',)
    search_fields = ('label', 'customer_name', 'customer_phone')
    readonly_fields = ('cart_json', 'created_at')


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = ('name', 'promo_type', 'percent', 'qty_required', 'qty_free',
                    'category', 'is_active', 'valid_from', 'valid_until')
    list_filter = ('promo_type', 'is_active', 'category')
    search_fields = ('name',)
    filter_horizontal = ('target_products',)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'tags', 'inn', 'created_at')
    search_fields = ('name', 'phone', 'inn')


class SaleInline(admin.TabularInline):
    model = Sale
    extra = 0
    readonly_fields = ('variant', 'quantity', 'sale_price', 'cost_at_sale')


@admin.register(SaleTransaction)
class SaleTransactionAdmin(admin.ModelAdmin):
    list_display = ('pk', 'branch', 'sold_by', 'payment_method',
                    'customer_name', 'sold_at')
    list_filter = ('branch', 'payment_method', 'sold_at')
    search_fields = ('customer_name', 'customer_phone')
    inlines = [SaleInline]


@admin.register(Return)
class ReturnAdmin(admin.ModelAdmin):
    list_display = ('refunded_at', 'sale', 'quantity', 'refund_amount', 'refunded_by')
    list_filter = ('refunded_at',)
    search_fields = ('sale__variant__product__code', 'reason')


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'username_snapshot', 'action',
                    'model_name', 'object_repr', 'ip')
    list_filter = ('action', 'model_name', 'created_at')
    search_fields = ('username_snapshot', 'object_repr', 'object_id', 'ip')
    readonly_fields = ('user', 'username_snapshot', 'action', 'model_name',
                       'object_id', 'object_repr', 'changes', 'ip', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(User)
class UserAdminCustom(UserAdmin):
    list_display = ('username', 'email', 'role', 'branch', 'is_active', 'is_superuser')
    list_filter = ('role', 'branch', 'is_active')
    fieldsets = UserAdmin.fieldsets + (
        ('Rol va Filial', {'fields': ('role', 'branch')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Rol va Filial', {'fields': ('role', 'branch')}),
    )


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ('name', 'address', 'phone', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'address')


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 1


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'external_barcode', 'category', 'default_sale_price')
    search_fields = ('code', 'name', 'external_barcode')
    list_display = ('code', 'name', 'category', 'total_stock', 'created_at')
    search_fields = ('code', 'name')
    list_filter = ('category',)
    inlines = [ProductVariantInline]
    readonly_fields = ('code', 'created_at')


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ('product', 'size', 'color')
    list_filter = ('product__category',)
    search_fields = ('product__code', 'product__name', 'size', 'color')


@admin.register(BranchStock)
class BranchStockAdmin(admin.ModelAdmin):
    list_display = ('branch', 'variant', 'stock_count', 'cost_price')
    list_filter = ('branch',)
    search_fields = ('variant__product__code', 'variant__product__name')


@admin.register(Intake)
class IntakeAdmin(admin.ModelAdmin):
    list_display = ('variant', 'branch', 'quantity', 'cost_per_unit', 'supplier',
                    'received_by', 'received_at')
    list_filter = ('branch', 'received_at')
    search_fields = ('variant__product__code', 'supplier')


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ('variant', 'branch', 'quantity', 'sale_price', 'sold_by', 'sold_at')
    list_filter = ('branch', 'sold_at')
    search_fields = ('variant__product__code',)
