# python imports
from urlparse import urlparse

# django imports
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.utils.translation import ugettext_lazy as _
from django.template.loader import render_to_string
from django.utils import simplejson
from django.http import HttpResponse

# lfs imports
import lfs
from lfs.checkout.settings import INVOICE_PREFIX, SHIPPING_PREFIX
from lfs.core.settings import LFS_ADDRESS_L10N
from lfs.customer import utils as customer_utils
from lfs.customer.forms import EmailForm
from lfs.customer.forms import RegisterForm
from lfs.customer.forms import AddressForm
from lfs.customer.models import Address
from lfs.order.models import Order

# other imports
from postal.library import get_postal_form_class
from postal.forms import PostalAddressForm
from postal.models import PostalAddress
from countries.models import Country

def login(request, template_name="lfs/customer/login.html"):
    """Custom view to login or register/login a user.

    The reason to use a custom login method are:

      * validate checkout type
      * integration of register and login form

    It uses Django's standard AuthenticationForm, though.
    """
    shop = lfs.core.utils.get_default_shop()

    # If only anonymous checkout is allowed this view doesn't exists :)
    # if shop.checkout_type == CHECKOUT_TYPE_ANON:
    #     raise Http404()

    # Using Djangos default AuthenticationForm
    login_form = AuthenticationForm()
    login_form.fields["username"].label = _(u"E-Mail")
    register_form = RegisterForm()

    if request.POST.get("action") == "login":
        login_form = AuthenticationForm(data=request.POST)

        if login_form.is_valid():
            redirect_to = request.POST.get("next")
            # Light security check -- make sure redirect_to isn't garbage.
            if not redirect_to or '//' in redirect_to or ' ' in redirect_to:
                redirect_to = reverse("lfs_shop_view")

            from django.contrib.auth import login
            login(request, login_form.get_user())

            return lfs.core.utils.set_message_cookie(
                redirect_to, msg = _(u"You have been logged in."))

    elif request.POST.get("action") == "register":
        register_form = RegisterForm(data=request.POST)
        if register_form.is_valid():

            email = register_form.data.get("email")
            password = register_form.data.get("password_1")

            # Create user
            user = User.objects.create_user(
                username=email, email=email, password=password)

            # Create customer
            customer = customer_utils.get_or_create_customer(request)
            customer.user = user

            # Notify
            lfs.core.signals.customer_added.send(user)

            # Log in user
            from django.contrib.auth import authenticate
            user = authenticate(username=email, password=password)

            from django.contrib.auth import login
            login(request, user)

            redirect_to = request.POST.get("next")
            if not redirect_to or '//' in redirect_to or ' ' in redirect_to:
                redirect_to = reverse("lfs_shop_view")

            return lfs.core.utils.set_message_cookie(
                redirect_to, msg = _(u"You have been registered and logged in."))

    # Get next_url
    next_url = request.REQUEST.get("next")
    if next_url is None:
        next_url = request.META.get("HTTP_REFERER")
    if next_url is None:
        next_url =  reverse("lfs_shop_view")

    # Get just the path of the url. See django.contrib.auth.views.login for more
    next_url = urlparse(next_url)
    next_url = next_url[2]

    try:
        login_form_errors = login_form.errors["__all__"]
    except KeyError:
        login_form_errors = None

    return render_to_response(template_name, RequestContext(request, {
        "login_form" : login_form,
        "login_form_errors" : login_form_errors,
        "register_form" : register_form,
        "next_url" : next_url,
    }))

def logout(request):
    """Custom method to logout a user.

    The reason to use a custom logout method is just to provide a login and a
    logoutmethod on one place.
    """
    from django.contrib.auth import logout
    logout(request)

    return lfs.core.utils.set_message_cookie(reverse("lfs_shop_view"),
        msg = _(u"You have been logged out."))

@login_required
def orders(request, template_name="lfs/customer/orders.html"):
    """Displays the orders of the current user
    """
    orders = Order.objects.filter(user=request.user)

    return render_to_response(template_name, RequestContext(request, {
        "orders" : orders,
    }))

@login_required
def order(request, id, template_name="lfs/customer/order.html"):
    """
    """
    orders = Order.objects.filter(user=request.user)
    order = get_object_or_404(Order, pk=id, user=request.user)

    return render_to_response(template_name, RequestContext(request, {
        "current_order" : order,
        "orders" : orders,
    }))

@login_required
def account(request, template_name="lfs/customer/account.html"):
    """Displays the main screen of the current user's account.
    """
    user = request.user

    return render_to_response(template_name, RequestContext(request, {
        "user" : user,
    }))

@login_required
def addresses(request, template_name="lfs/customer/addresses.html"):
    """Provides a form to edit addresses and bank account.
    """
    customer = lfs.customer.utils.get_customer(request)
    shop = lfs.core.utils.get_default_shop()

    if request.method == "POST":
        form = AddressForm(request.POST)
        if form.is_valid():
            save_postal_address(request, customer, INVOICE_PREFIX)
            save_postal_address(request, customer, SHIPPING_PREFIX)
            customer.selected_invoice_address.customer = customer
            customer.selected_invoice_address.firstname = form.cleaned_data['invoice_firstname']
            customer.selected_invoice_address.lastname = form.cleaned_data['invoice_lastname']
            customer.selected_invoice_address.phone = form.cleaned_data['invoice_phone']
            customer.selected_invoice_address.email = form.cleaned_data['invoice_email']
            customer.selected_invoice_address.save()
            customer.selected_shipping_address.customer = customer
            customer.selected_shipping_address.firstname = form.cleaned_data['shipping_firstname']
            customer.selected_shipping_address.lastname = form.cleaned_data['shipping_lastname']
            customer.selected_shipping_address.phone = form.cleaned_data['shipping_phone']
            customer.selected_shipping_address.email = form.cleaned_data['shipping_email']
            customer.selected_shipping_address.save()
            return HttpResponseRedirect(reverse("lfs_my_addresses"))
    else:
        initial = {}
        if customer:
            if customer.selected_invoice_address is not None:
                initial.update({"invoice_firstname": customer.selected_invoice_address.firstname,
                                "invoice_lastname": customer.selected_invoice_address.lastname,
                                "invoice_phone": customer.selected_invoice_address.phone,
                                "invoice_email": customer.selected_invoice_address.email,
                                })
            if customer.selected_shipping_address is not None:
                initial.update({"shipping_firstname": customer.selected_shipping_address.firstname,
                                "shipping_lastname": customer.selected_shipping_address.lastname,
                                "shipping_phone": customer.selected_shipping_address.phone,
                                "shipping_email": customer.selected_shipping_address.email,
                                })

        form = AddressForm(initial=initial)
    return render_to_response(template_name, RequestContext(request, {
        "form": form,
        "shipping_address_inline" : address_inline(request, "shipping", form),
        "invoice_address_inline" : address_inline(request, "invoice", form),
    }))

def get_country_code(request, prefix):
    # get country_code from the request
    country_code = request.POST.get(prefix + '-country', '')

    # get country code from customer
    if country_code == '':
        customer = customer_utils.get_or_create_customer(request)
        if prefix == INVOICE_PREFIX:
            if customer.selected_invoice_address is not None:
                if customer.selected_invoice_address.postal_address is not None:
                    if customer.selected_invoice_address.postal_address.country is not None:
                        country_code = customer.selected_invoice_address.postal_address.country.iso
        elif prefix == SHIPPING_PREFIX:
            if customer.selected_shipping_address is not None:
                if customer.selected_shipping_address.postal_address is not None:
                    if customer.selected_shipping_address.postal_address.country is not None:
                        country_code = customer.selected_shipping_address.postal_address.country.iso

    # get country code from shop
    if country_code == '':
        shop = lfs.core.utils.get_default_shop()
        if shop.default_country is not None:
            country_code = shop.default_country.iso
    return country_code


def address_inline(request, prefix, form):
    """displays the invoice address with localized fields
    """
    template_name="lfs/customer/" + prefix + "_address_inline.html"
    country_code = get_country_code(request, prefix)
    if country_code != '':
        shop = lfs.core.utils.get_default_shop()
        countries = None
        if prefix == INVOICE_PREFIX:
            countries = shop.invoice_countries.all()
        else:
            countries = shop.shipping_countries.all()
        customer = customer_utils.get_or_create_customer(request)
        address_form_class = get_postal_form_class(country_code)
        if request.method == 'POST':
            if LFS_ADDRESS_L10N == True:
                address_form = address_form_class(prefix=prefix, data=request.POST,)
            else:
                address_form = PostalAddressForm(prefix=prefix, data=request.POST,)
            if countries is not None:
                address_form.fields["country"].choices = [(c.iso, c.name) for c in countries]
            save_postal_address(request, customer, prefix)
        else:
             # If there are addresses intialize the form.
            initial = {}
            postal_address = None
            if hasattr(customer, 'selected_' + prefix + '_address'):
                customer_selected_address = getattr(customer, 'selected_' + prefix + '_address')
                if customer_selected_address is not None:
                    postal_address = customer_selected_address.postal_address
            if postal_address is not None:
                initial.update({
                    "line1" : postal_address.line1,
                    "line2" : postal_address.line2,
                    "city" : postal_address.city,
                    "state" : postal_address.state,
                    "code" : postal_address.code,
                    "country" : postal_address.country.iso,
                })
            else:
                initial.update({prefix + "-country" : country_code,})
            address_form = address_form_class(prefix=prefix, initial=initial)
            if countries is not None:
                address_form.fields["country"].choices = [(c.iso, c.name) for c in countries]

    # Removes fields from address form if requested via settings.
    for i in range(1, 6):
        address_settings = getattr(settings, "POSTAL_ADDRESS_LINE%s" % i, None)
        try:
            if address_settings and address_settings[2] == False:
                del address_form.fields["line%s" % i]
        except IndexError:
            pass

    # if request via ajax don't display validity errors
    if request.is_ajax():
        address_form._errors = {}

    return render_to_string(template_name, RequestContext(request, {
        "address_form": address_form,
        "form": form,
    }))

def save_postal_address(request, customer, prefix):
    # get the shop
    shop = lfs.core.utils.get_default_shop()

    # get the country for the address
    country_iso = request.POST.get(prefix + "-country", shop.default_country.iso)
    selected_country = Country.objects.get(iso=country_iso)

    customer_selected_address = None
    address_attribute = 'selected_' + prefix + '_address'
    postal_address = None
    postal_address_form = None
    existing_address = False
    existing_postal_address = False
    if hasattr(customer, address_attribute):
        customer_selected_address = getattr(customer, address_attribute)
        if customer_selected_address is not None:
            existing_address = True
            if customer_selected_address.postal_address is not None:
                # we have an existing address so we update it
                exising_postal_address = True
                postal_address = customer_selected_address.postal_address
                postal_address.line1 = request.POST.get(prefix + "-line1")
                postal_address.line2 = request.POST.get(prefix + "-line2")
                postal_address.city = request.POST.get(prefix + "-city")
                postal_address.state = request.POST.get(prefix + "-state")
                postal_address.code = request.POST.get(prefix + "-code")
                postal_address.country = selected_country
                postal_address.save()
                postal_address_form = PostalAddressForm(prefix=prefix,data=request.POST, instance=postal_address)
    if not existing_address:
        # no address exists for customer so create one
        customer_selected_address = Address.objects.create(customer=customer)
    if not existing_postal_address:
        postal_address_form = PostalAddressForm(prefix=prefix,data=request.POST)
        postal_address, created = PostalAddress.objects.get_or_create(line1=request.POST.get(prefix + "-line1"),
                                                       line2=request.POST.get(prefix + "-line2"),
                                                       city=request.POST.get(prefix + "-city"),
                                                       state=request.POST.get(prefix + "-state"),
                                                       code=request.POST.get(prefix + "-code"),
                                                       country=selected_country)
    if customer_selected_address is not None:
        if postal_address is not None:
            setattr(customer_selected_address, 'postal_address', postal_address)
            customer_selected_address.save()
        setattr(customer, address_attribute, customer_selected_address)
        customer.save()
    return postal_address

def email(request, template_name="lfs/customer/email.html"):
    """Saves the email address from the data form.
    """
    if request.method == "POST":
        email_form = EmailForm(initial={"email" : request.user.email}, data=request.POST)
        if email_form.is_valid():
            request.user.email = email_form.cleaned_data.get("email")
            request.user.save()
            return HttpResponseRedirect(reverse("lfs_my_email"))
    else:
        email_form = EmailForm(initial={"email" : request.user.email})

    return render_to_response(template_name, RequestContext(request, {
        "email_form": email_form
    }))

def password(request, template_name="lfs/customer/password.html"):
    """Changes the password of current user.
    """
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect(reverse("lfs_my_password"))
    else:
        form = PasswordChangeForm(request.user)

    return render_to_response(template_name, RequestContext(request, {
        "form" : form
    }))

