from django import forms
from django.contrib.auth.forms import AuthenticationForm
from .models import User, Branch, Product, Category, ProductVariant


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        label='Foydalanuvchi nomi',
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Foydalanuvchi nomi',
            'autofocus': True,
        })
    )
    password = forms.CharField(
        label='Parol',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Parol',
        })
    )


class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = ['name', 'address', 'phone', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'name': 'Filial nomi',
            'address': 'Manzil',
            'phone': 'Telefon',
            'is_active': 'Faol',
        }


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ['name', 'category', 'description', 'image',
                  'default_sale_price', 'markup_percent']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/*',
                'capture': 'environment',  # Phone: open rear camera directly
            }),
            'default_sale_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '1'}),
            'markup_percent': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1'}),
        }
        labels = {
            'name': 'Nomi',
            'category': 'Kategoriya',
            'description': 'Tavsif',
            'image': 'Rasm',
            'default_sale_price': "Sotuv narxi (so'm)",
            'markup_percent': "Standart foiz markup (%)",
        }


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name']
        widgets = {'name': forms.TextInput(attrs={'class': 'form-control'})}
        labels = {'name': 'Kategoriya nomi'}


class IntakeForm(forms.Form):
    branch = forms.ModelChoiceField(
        label='Qabul filiali',
        queryset=Branch.objects.filter(is_active=True),
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    size = forms.CharField(
        label="O'lcham",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': "42 yoki XL"})
    )
    color = forms.CharField(
        label='Rang',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': "Qora, Ko'k..."})
    )
    quantity = forms.IntegerField(
        label='Soni', min_value=1,
        widget=forms.NumberInput(attrs={'class': 'form-control'})
    )
    cost_per_unit = forms.DecimalField(
        label="Tannarx (1 dona, so'm)", min_value=0, max_digits=12, decimal_places=2,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '1',
                                        'id': 'id_cost_per_unit'})
    )
    markup_percent = forms.DecimalField(
        label="Foiz markup (%)", min_value=0, max_digits=6, decimal_places=2,
        required=False, initial=40,
        help_text="Sotuv narxi = tannarx × (1 + foiz/100)",
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1',
                                        'id': 'id_markup_percent'})
    )
    sale_price = forms.DecimalField(
        label="Sotuv narxi (1 dona, so'm)", min_value=0, max_digits=12, decimal_places=2,
        required=False,
        help_text="Avtomatik hisoblanadi yoki o'zingiz kiriting",
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '1',
                                        'id': 'id_sale_price'})
    )
    update_product_price = forms.BooleanField(
        label="Mahsulotning umumiy sotuv narxini ham yangilash",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )
    supplier = forms.CharField(
        label='Yetkazib beruvchi', required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    note = forms.CharField(
        label='Izoh', required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2})
    )

    def clean(self):
        cd = super().clean()
        cost = cd.get('cost_per_unit')
        markup = cd.get('markup_percent')
        sale = cd.get('sale_price')
        if cost is not None:
            if (sale is None or sale == 0) and markup is not None:
                cd['sale_price'] = (cost * (1 + markup / 100)).quantize(cost)
            elif sale and sale > 0:
                pass
            else:
                cd['sale_price'] = cost
        return cd


class SaleForm(forms.Form):
    quantity = forms.IntegerField(
        label='Soni', min_value=1,
        widget=forms.NumberInput(attrs={'class': 'form-control'})
    )
    sale_price = forms.DecimalField(
        label="Sotuv narxi (1 dona, so'm)", min_value=0, max_digits=12, decimal_places=2,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'})
    )
    note = forms.CharField(
        label='Izoh', required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )


class UserCreateForm(forms.ModelForm):
    password = forms.CharField(
        label='Parol', min_length=4,
        widget=forms.PasswordInput(attrs={'class': 'form-control'})
    )

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'role', 'branch', 'is_active']
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'branch': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'username': 'Foydalanuvchi nomi',
            'first_name': 'Ism', 'last_name': 'Familiya',
            'email': 'Email', 'role': 'Rol', 'branch': 'Filial',
            'is_active': 'Faol',
        }

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password'])
        if commit:
            user.save()
        return user


class UserEditForm(forms.ModelForm):
    new_password = forms.CharField(
        label='Yangi parol (faqat o\'zgartirish uchun)', required=False, min_length=4,
        widget=forms.PasswordInput(attrs={'class': 'form-control',
                                          'placeholder': "Bo'sh qoldiring — o'zgarmaydi"})
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'role', 'branch', 'is_active']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'branch': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'first_name': 'Ism', 'last_name': 'Familiya',
            'email': 'Email', 'role': 'Rol', 'branch': 'Filial',
            'is_active': 'Faol',
        }

    def save(self, commit=True):
        user = super().save(commit=False)
        pw = self.cleaned_data.get('new_password')
        if pw:
            user.set_password(pw)
        if commit:
            user.save()
        return user


REPORT_TYPES = [
    ('sales', 'Sotuvlar'),
    ('intakes', 'Qabullar'),
    ('inventory', 'Joriy ombor (hozirgi holat)'),
    ('by_product', 'Mahsulotlar bo\'yicha xulosa'),
]

PERIOD_CHOICES = [
    ('today', 'Bugun'),
    ('week', 'Oxirgi 7 kun'),
    ('month', 'Oxirgi 30 kun'),
    ('this_month', 'Joriy oy'),
    ('custom', 'Aniq sanalar'),
]


class ReportForm(forms.Form):
    report_type = forms.ChoiceField(
        label='Hisobot turi', choices=REPORT_TYPES,
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    period = forms.ChoiceField(
        label='Davr', choices=PERIOD_CHOICES, initial='week',
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    date_from = forms.DateField(
        label="Boshlanish sanasi", required=False,
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'})
    )
    date_to = forms.DateField(
        label="Tugash sanasi", required=False,
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'})
    )
    branch = forms.ModelChoiceField(
        label='Filial', queryset=Branch.objects.all(),
        required=False, empty_label='— Barcha filiallar —',
        widget=forms.Select(attrs={'class': 'form-select'})
    )
