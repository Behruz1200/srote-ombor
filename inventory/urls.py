from django.urls import path
from . import views

urlpatterns = [
    # PWA — manifest and service worker at root scope
    path('manifest.webmanifest', views.manifest, name='manifest'),
    path('sw.js', views.service_worker, name='service_worker'),

    path('healthz', views.healthz, name='healthz'),
    path('', views.home, name='home'),

    # --- Onlayn do'kon (ochiq sayt) — SHOP-1/SHOP-2: KANAL YOPIQ ---
    # Uchinchi auditda ham ochiq turgan yagona kritik: /shop/order/<pk>/ har bir
    # mijozning ismi/telefoni/manzilini autentifikatsiyasiz ochib qo'yardi va
    # /shop/checkout/ zaxira bron qilmasдан, pul olмасдан buyurtma qabul qilardi.
    # Marshrutlarни BUTUNLAY olib tashlaymiz (ShopClosedMiddleware'ga qo'shimcha,
    # ikki qavatli himoya). Kanal to'g'ri qurilганда (zaxira bron + to'lov + xabar)
    # bu bloknи ochamiz.
    # path('shop/', views.shop_home, name='shop_home'),
    # path('shop/catalog/', views.shop_catalog, name='shop_catalog'),
    # path('shop/p/<str:code>/', views.shop_product, name='shop_product'),
    # path('shop/cart/', views.shop_cart, name='shop_cart'),
    # path('shop/cart/add/', views.shop_cart_add, name='shop_cart_add'),
    # path('shop/cart/update/', views.shop_cart_update, name='shop_cart_update'),
    # path('shop/checkout/', views.shop_checkout, name='shop_checkout'),
    # path('shop/order/<int:pk>/', views.shop_order_done, name='shop_order_done'),
    # Xodimlar uchun
    path('web-orders/', views.web_orders, name='web_orders'),
    path('web-orders/<int:pk>/status/', views.web_order_status,
         name='web_order_status'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('login/2fa/', views.login_2fa, name='login_2fa'),
    path('security/2fa/', views.security_2fa, name='security_2fa'),
    path('lookup/', views.lookup, name='lookup'),

    # Mijoz so'rovlari (customer product-request / demand log)
    path('requests/', views.product_requests, name='product_requests'),
    path('requests/add/', views.product_request_add, name='product_request_add'),
    path('requests/resolve/', views.product_request_resolve, name='product_request_resolve'),

    # Stocktake (physical inventory count)
    path('stocktake/', views.stocktake_list, name='stocktake_list'),
    path('stocktake/new/', views.stocktake_create, name='stocktake_create'),
    path('stocktake/<int:pk>/', views.stocktake_detail, name='stocktake_detail'),

    # Inter-branch stock transfers
    path('transfers/', views.transfer_list, name='transfer_list'),
    path('transfers/new/', views.transfer_create, name='transfer_create'),
    path('transfers/<int:pk>/', views.transfer_detail, name='transfer_detail'),
    path('transfers/<int:pk>/receive/', views.transfer_receive, name='transfer_receive'),

    # Stock write-off (damage / loss / spoilage) — admin only (STK-1)
    path('writeoff/', views.writeoff_list, name='writeoff_list'),

    # Shifts (open/close, cash reconciliation)
    path('shift/open/', views.shift_open, name='shift_open'),
    path('shift/close/', views.shift_close, name='shift_close'),
    path('shift/cash-out/', views.cash_payout, name='cash_payout'),
    path('shift/cash-in/', views.cash_in, name='cash_in'),
    path('shift/<int:pk>/receipt/', views.shift_receipt, name='shift_receipt'),
    path('shift/<int:pk>/returns/', views.shift_returns, name='shift_returns'),
    path('shift/<int:pk>/', views.shift_detail, name='shift_detail'),
    path('shifts/', views.shift_list, name='shift_list'),

    # POS terminal (BILLZ-style single-page scanner workflow)
    path('pos/', views.pos_terminal, name='pos_terminal'),
    path('pos/lookup/', views.pos_lookup, name='pos_lookup'),
    path('pos/catalog/', views.pos_catalog, name='pos_catalog'),   # OFF-8
    path('pos/device-sync/', views.pos_device_sync, name='pos_device_sync'),   # OFF-10
    path('pos/checkout/', views.pos_checkout, name='pos_checkout'),
    path('pos/display/', views.pos_customer_display, name='pos_customer_display'),
    path('pos/park/', views.pos_park, name='pos_park'),
    path('pos/parked/<int:pk>/resume/', views.pos_parked_resume, name='pos_parked_resume'),
    path('pos/parked/<int:pk>/delete/', views.pos_parked_delete, name='pos_parked_delete'),
    path('pos/txn/<int:pk>/refundable/', views.pos_txn_refundable, name='pos_txn_refundable'),
    path('pos/refund/', views.pos_refund, name='pos_refund'),
    path('pos/exchange/', views.pos_exchange, name='pos_exchange'),
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
    path('products/resolve-name/', views.product_resolve_name, name='product_resolve_name'),
    path('employee-debts/', views.employee_debt_list, name='employee_debt_list'),
    path('products/export/', views.product_export, name='product_export'),
    path('products/new/', views.product_create, name='product_create'),
    path('products/merge/', views.product_merge, name='product_merge'),
    path('products/search-suggest/', views.product_search_suggest,
         name='product_search_suggest'),
    path('products/search-for-attach/', views.product_search_for_attach,
         name='product_search_for_attach'),
    path('products/attach-barcode/', views.product_attach_barcode,
         name='product_attach_barcode'),
    path('products/<str:code>/', views.product_detail, name='product_detail'),
    path('products/<str:code>/edit/', views.product_edit, name='product_edit'),
    path('products/<str:code>/image/', views.product_image, name='product_image'),
    path('products/<str:code>/delete/', views.product_delete,
         name='product_delete'),
    path('products/<str:code>/variants/edit/', views.product_variants_edit,
         name='product_variants_edit'),
    path('products/<str:code>/variants/move/', views.product_variants_move,
         name='product_variants_move'),
    path('variants/split/', views.variant_split_batch, name='variant_split_batch'),
    path('variants/<int:pk>/delete/', views.variant_delete,
         name='variant_delete'),
    path('products/<str:code>/intake/', views.intake_for_product, name='intake_for_product'),

    path('intake/', views.intake_new, name='intake_new'),
    path('intake/variants/', views.intake_variants, name='intake_variants'),
    path('intake/clothes/', views.clothes_intake, name='clothes_intake'),
    # Aralash qabul — kiyim (o'lchamlar) + oddiy tovar bir sahifada
    path('intake/mixed/', views.intake_mixed, name='intake_mixed'),
    path('intake/mixed/save/', views.intake_mixed_save, name='intake_mixed_save'),
    # Faktura rasmidan qabul (AI o'qiydi, foydalanuvchi tasdiqlaydi)
    path('intake/photo/', views.intake_photo, name='intake_photo'),
    path('intake/photo/extract/', views.intake_photo_extract, name='intake_photo_extract'),
    path('intake/photo/save/', views.intake_photo_save, name='intake_photo_save'),
    path('intake/photo/draft/', views.intake_photo_draft, name='intake_photo_draft'),
    path('intake/photo/draft/<int:pk>/delete/', views.intake_photo_draft_delete,
         name='intake_photo_draft_delete'),
    path('labels/variants/', views.variant_labels, name='variant_labels'),
    path('labels/price/', views.price_labels, name='price_labels'),

    # --- Narxlar (narx / marja / ulgurji / aksiyalar) ---
    path('prices/', views.price_list, name='price_list'),
    path('prices/apply/', views.price_apply, name='price_apply'),
    path('prices/history/', views.price_history, name='price_history'),
    path('prices/quick-sell/', views.quick_sell_settings, name='quick_sell_settings'),
    path('prices/promotions/', views.promotion_list, name='promotion_list'),
    path('prices/promotions/save/', views.promotion_save, name='promotion_save'),
    path('prices/promotions/<int:pk>/delete/', views.promotion_delete,
         name='promotion_delete'),
    path('intake/import/', views.intake_import, name='intake_import'),
    path('intake/import/template/', views.intake_import_template,
         name='intake_import_template'),
    path('intake/lookup/', views.intake_lookup, name='intake_lookup'),
    path('intake/supplier-search/', views.intake_supplier_search, name='intake_supplier_search'),
    path('intake/sessions/<int:pk>/', views.intake_session_detail, name='intake_session_detail'),
    path('intake/suppliers/', views.supplier_list, name='supplier_list'),
    # --- Ombor (to'liq nazorat va tahlil) ---
    path('warehouse/', views.warehouse, name='warehouse'),
    path('warehouse/adjust/', views.warehouse_adjust, name='warehouse_adjust'),
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
    path('transaction/<uuid:token>/', views.transaction_detail, name='transaction_detail'),

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
]
