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

    # Stocktake (physical inventory count)
    path('stocktake/', views.stocktake_list, name='stocktake_list'),
    path('stocktake/new/', views.stocktake_create, name='stocktake_create'),
    path('stocktake/<int:pk>/', views.stocktake_detail, name='stocktake_detail'),

    # Inter-branch stock transfers
    path('transfers/', views.transfer_list, name='transfer_list'),
    path('transfers/new/', views.transfer_create, name='transfer_create'),
    path('transfers/<int:pk>/', views.transfer_detail, name='transfer_detail'),
    path('transfers/<int:pk>/receive/', views.transfer_receive, name='transfer_receive'),

    # Shifts (open/close, cash reconciliation)
    path('shift/open/', views.shift_open, name='shift_open'),
    path('shift/close/', views.shift_close, name='shift_close'),
    path('shift/<int:pk>/', views.shift_detail, name='shift_detail'),
    path('shifts/', views.shift_list, name='shift_list'),

    # POS terminal (BILLZ-style single-page scanner workflow)
    path('pos/', views.pos_terminal, name='pos_terminal'),
    path('pos/lookup/', views.pos_lookup, name='pos_lookup'),
    path('pos/checkout/', views.pos_checkout, name='pos_checkout'),
    path('pos/display/', views.pos_customer_display, name='pos_customer_display'),
    path('pos/park/', views.pos_park, name='pos_park'),
    path('pos/parked/<int:pk>/resume/', views.pos_parked_resume, name='pos_parked_resume'),
    path('pos/parked/<int:pk>/delete/', views.pos_parked_delete, name='pos_parked_delete'),
    path('pos/txn/<int:pk>/refundable/', views.pos_txn_refundable, name='pos_txn_refundable'),
    path('pos/refund/', views.pos_refund, name='pos_refund'),
    path('pos/unlock/', views.pos_unlock, name='pos_unlock'),
    path('pos/payment/intent/', views.pos_payment_intent, name='pos_payment_intent'),
    path('pos/payment/status/', views.pos_payment_status, name='pos_payment_status'),
    path('pos/promo-eval/', views.pos_promo_eval, name='pos_promo_eval'),
    path('pos/qr/<int:pk>/', views.pos_static_qr, name='pos_static_qr'),
    path('pos/payment/create/', views.pos_payment_create, name='pos_payment_create'),
    path('pos/payment/check/<int:pk>/', views.pos_payment_check, name='pos_payment_check'),
    path('payments/webhook/<str:provider>/', views.payments_webhook, name='payments_webhook'),
    path('payment-qrs/', views.payment_qr_list, name='payment_qr_list'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/send-daily/', views.send_daily_summary_now, name='send_daily_summary_now'),

    path('products/', views.product_list, name='product_list'),
    path('products/bulk/', views.product_bulk_update, name='product_bulk_update'),
    path('products/new/', views.product_create, name='product_create'),
    path('products/search-for-attach/', views.product_search_for_attach,
         name='product_search_for_attach'),
    path('products/attach-barcode/', views.product_attach_barcode,
         name='product_attach_barcode'),
    path('products/<str:code>/', views.product_detail, name='product_detail'),
    path('products/<str:code>/edit/', views.product_edit, name='product_edit'),
    path('products/<str:code>/intake/', views.intake_for_product, name='intake_for_product'),

    path('intake/', views.intake_new, name='intake_new'),
    path('intake/quick/', views.intake_quick, name='intake_quick'),
    path('intake/quick/save/', views.intake_quick_save, name='intake_quick_save'),
    path('intake/lookup/', views.intake_lookup, name='intake_lookup'),
    path('intake/supplier-search/', views.intake_supplier_search, name='intake_supplier_search'),
    path('intake/sessions/<int:pk>/', views.intake_session_detail, name='intake_session_detail'),
    path('intake/suppliers/', views.supplier_list, name='supplier_list'),
    path('reorder/', views.reorder_page, name='reorder_page'),
    path('cashier/<int:user_id>/', views.cashier_stats, name='cashier_stats'),
    path('import/', views.csv_import, name='csv_import'),

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
