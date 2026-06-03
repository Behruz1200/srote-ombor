from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    User, Branch, Category, Product, ProductVariant,
    BranchStock, Intake, Sale, AuditLog, SaleTransaction, Return, Customer,
    ParkedSale,
)


@admin.register(ParkedSale)
class ParkedSaleAdmin(admin.ModelAdmin):
    list_display = ('id', 'label', 'branch', 'parked_by', 'created_at')
    list_filter = ('branch',)
    search_fields = ('label', 'customer_name', 'customer_phone')
    readonly_fields = ('cart_json', 'created_at')


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
