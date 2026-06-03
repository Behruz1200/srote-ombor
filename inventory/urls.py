from django.urls import path
from . import views

urlpatterns = [
    # PWA — manifest and service worker at root scope
    path('manifest.webmanifest', views.manifest, name='manifest'),
    path('sw.js', views.service_worker, name='service_worker'),

    path('healthz', views.healthz, name='healthz'),
    path('', views.home, name='home'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('lookup/', views.lookup, name='lookup'),

    # Shifts (open/close, cash reconciliation)
    path('shift/open/', views.shift_open, name='shift_open'),
    path('shift/close/', views.shift_close, name='shift_close'),
    path('shift/<int:pk>/', views.shift_detail, name='shift_detail'),
    path('shifts/', views.shift_list, name='shift_list'),

    # POS terminal (BILLZ-style single-page scanner workflow)
    path('pos/', views.pos_terminal, name='pos_terminal'),
    path('pos/lookup/', views.pos_lookup, name='pos_lookup'),
    path('pos/checkout/', views.pos_checkout, name='pos_checkout'),
    path('dashboard/', views.dashboard, name='dashboard'),

    path('products/', views.product_list, name='product_list'),
    path('products/new/', views.product_create, name='product_create'),
    path('products/<str:code>/', views.product_detail, name='product_detail'),
    path('products/<str:code>/edit/', views.product_edit, name='product_edit'),
    path('products/<str:code>/intake/', views.intake_for_product, name='intake_for_product'),

    path('intake/', views.intake_new, name='intake_new'),

    path('sale/<int:stock_id>/', views.sale_create, name='sale_create'),
    path('sales/', views.sales_list, name='sales_list'),

    # Cart / multi-item sale
    path('cart/', views.cart_view, name='cart_view'),
    path('cart/add/<int:stock_id>/', views.cart_add, name='cart_add'),
    path('cart/update/', views.cart_update, name='cart_update'),
    path('cart/clear/', views.cart_clear, name='cart_clear'),
    path('checkout/', views.checkout, name='checkout'),
    path('transaction/<int:pk>/', views.transaction_detail, name='transaction_detail'),

    # Returns
    path('sale/<int:sale_id>/return/', views.return_create, name='return_create'),

    path('categories/', views.category_list, name='category_list'),

    path('branches/', views.branch_list, name='branch_list'),
    path('branches/new/', views.branch_create, name='branch_create'),
    path('branches/<int:pk>/edit/', views.branch_edit, name='branch_edit'),

    path('users/', views.user_list, name='user_list'),
    path('users/new/', views.user_create, name='user_create'),
    path('users/<int:pk>/edit/', views.user_edit, name='user_edit'),

    path('customers/', views.customer_list, name='customer_list'),
    path('customers/<int:pk>/', views.customer_detail, name='customer_detail'),
    path('pos/customer/', views.pos_customer_lookup, name='pos_customer_lookup'),

    path('reports/', views.reports, name='reports'),
    path('insights/', views.insights, name='insights'),
    path('audit/', views.audit_list, name='audit_list'),

    path('products/<str:code>/qr.png', views.qr_image, name='qr_image'),
    path('products/<str:code>/label/', views.product_labels, name='product_label'),
    path('labels/', views.product_labels, name='labels'),
]
