from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Branch, Category, Product, ProductVariant, BranchStock, Intake, Sale


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
