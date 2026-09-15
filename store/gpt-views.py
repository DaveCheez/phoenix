from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse

from .models import Category, SubCategory, Product

def category_list(request):
    categories = Category.objects.all()
    return render(request, 'category_list.html', {'categories': categories})

# def category_detail(request, category_id):
#     category = get_object_or_404(Category, id=category_id)
#     return render(request, 'category_detail.html', {'category': category})

def subcategory_list(request, category_id):
    category = get_object_or_404(Category, id=category_id)
    subcategories = category.subcategories.all()
    return render(request, 'subcategory_list.html', {'subcategories': subcategories, 'category': category})

# def subcategory_detail(request, category_id, subcategory_id):
#     category = get_object_or_404(Category, id=category_id)
#     subcategory = get_object_or_404(SubCategory, id=subcategory_id, category=category)
#     products = subcategory.products.all()
#     return render(request, 'subcategory_detail.html', {'category': category, 'subcategory': subcategory, 'products': products})

def product_detail(request, category_id, subcategory_id, product_id):
    category = get_object_or_404(Category, id=category_id)
    subcategory = get_object_or_404(SubCategory, id=subcategory_id, category=category)
    product = get_object_or_404(Product, id=product_id, category=category, subcategory=subcategory)
    return render(request, 'product_detail.html', {'category': category, 'subcategory': subcategory, 'product': product})
