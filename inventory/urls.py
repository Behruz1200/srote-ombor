from django.urls import path
from . import views

urlpatterns = [
    # PWA — manifest and service worker at root scope
    path('manifest.webmanifest', views.manifest, name='manifest'),
    path('sw.js', views.service_worker, name='service_worker'),

    path('', views.home, name='home'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('lookup/', views.lookup, name='lookup'),
    path('dashboard/', views.dashboard, name='dashboard'),

    path('products/', views.product_list, name='product_list'),
    path('products/new/', views.product_create, name='product_create'),
    path('products/<str:code>/', views.product_detail, name='product_detail'),
    path('products/<str:code>/edit/', views.product_edit, name='product_edit'),
    path('products/<str:code>/intake/', views.intake_for_product, name='intake_for_product'),

    path('intake/', views.intake_new, name='intake_new'),

    path('sale/<int:stock_id>/', views.sale_create, name='sale_create'),
    path('sales/', views.sales_list, name='sales_list'),

    path('categories/', views.category_list, name='category_list'),

    path('branches/', views.branch_list, name='branch_list'),
    path('branches/new/', views.branch_create, name='branch_create'),
    path('branches/<int:pk>/edit/', views.branch_edit, name='branch_edit'),

    path('users/', views.user_list, name='user_list'),
    path('users/new/', views.user_create, name='user_create'),
    path('users/<int:pk>/edit/', views.user_edit, name='user_edit'),

    path('reports/', views.reports, name='reports'),
    path('insights/', views.insights, name='insights'),

    path('products/<str:code>/qr.png', views.qr_image, name='qr_image'),
    path('products/<str:code>/label/', views.product_labels, name='product_label'),
    path('labels/', views.product_labels, name='labels'),
]
