"""Account info extraction from Netflix account pages."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from .utils import (
    decode_netflix_value,
    extract_first_match,
    format_boolean_label,
    normalize_phone_number,
    normalize_plan_key,
    parse_boolean_value,
)


def extract_bool_value(response_text: str, patterns) -> Optional[str]:
    value = extract_first_match(response_text, patterns, re.IGNORECASE)
    if value is None:
        return None
    labeled = format_boolean_label(value)
    return labeled if labeled is not None else value


def extract_profile_names(response_text: str) -> Optional[str]:
    names = []
    for pattern in (
        r'"profileName"\s*:\s*"([^"]+)"',
        r'"profileName"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
    ):
        for found in re.findall(pattern, response_text, re.DOTALL):
            decoded = decode_netflix_value(found)
            if decoded and decoded not in names:
                names.append(decoded)
    for match in re.finditer(r'"__typename"\s*:\s*"Profile"', response_text):
        snippet = response_text[match.start() : match.start() + 1200]
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', snippet)
        if name_match:
            decoded = decode_netflix_value(name_match.group(1))
            if decoded and decoded not in names:
                names.append(decoded)
    return ", ".join(names) if names else None


def merge_info(primary: Optional[Dict], fallback: Optional[Dict]) -> Dict[str, Any]:
    merged = dict(fallback or {})
    for key, value in (primary or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def has_complete_account_info(info: Optional[Dict]) -> bool:
    if not info:
        return False
    required = (
        "countryOfSignup",
        "membershipStatus",
        "localizedPlanName",
        "maxStreams",
        "videoQuality",
    )
    return all(info.get(f) and info.get(f) != "null" for f in required)


def extract_info_from_graphql_payload(response_text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(response_text)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}

    growth_account = data.get("growthAccount") or {}
    current_profile = data.get("currentProfile") or {}
    current_plan = ((growth_account.get("currentPlan") or {}).get("plan") or {})
    next_plan = ((growth_account.get("nextPlan") or {}).get("plan") or {})
    next_billing = growth_account.get("nextBillingDate") or {}
    hold_meta = growth_account.get("growthHoldMetadata") or {}
    local_phone = growth_account.get("growthLocalizablePhoneNumber") or {}
    raw_phone = local_phone.get("rawPhoneNumber") or {}
    payment_methods = growth_account.get("growthPaymentMethods") or []
    payment_method = (
        payment_methods[0]
        if payment_methods and isinstance(payment_methods[0], dict)
        else {}
    )
    payment_logo = (payment_method.get("paymentOptionLogo") or {}).get("paymentOptionLogo")
    payment_typename = str(payment_method.get("__typename") or "")
    payment_display_text = decode_netflix_value(payment_method.get("displayText"))
    profiles = growth_account.get("profiles") or []

    phone_digits = None
    phone_verified = None
    phone_country = None
    if isinstance(raw_phone, dict):
        digits_obj = raw_phone.get("phoneNumberDigits") or {}
        phone_digits = (
            digits_obj.get("value")
            if isinstance(digits_obj, dict)
            else raw_phone.get("phoneNumberDigits")
        )
        phone_verified = raw_phone.get("isVerified")
        phone_country = raw_phone.get("countryCode")
    else:
        phone_digits = raw_phone

    def _growth_email(profile_obj):
        if not isinstance(profile_obj, dict):
            return None, None
        growth_email = profile_obj.get("growthEmail") or {}
        email_obj = growth_email.get("email") or {}
        email_value = email_obj.get("value") if isinstance(email_obj, dict) else None
        return email_value, growth_email.get("isVerified")

    email_value, email_verified = _growth_email(current_profile)
    if not email_value:
        for profile in profiles:
            email_value, email_verified = _growth_email(profile)
            if email_value:
                break

    profile_names = []
    for profile in profiles:
        if isinstance(profile, dict):
            name = decode_netflix_value(profile.get("name"))
            if name and name not in profile_names:
                profile_names.append(name)

    feature_types = []
    for plan_obj in (current_plan, next_plan):
        for feature in plan_obj.get("availableFeatures") or []:
            if isinstance(feature, dict) and feature.get("type"):
                feature_types.append(str(feature["type"]).upper())

    def _first_bool(*candidates):
        for candidate in candidates:
            labeled = format_boolean_label(candidate)
            if labeled is not None:
                return labeled
        return None

    def _price(plan_obj):
        if not isinstance(plan_obj, dict):
            return None
        for key in (
            "priceDisplay",
            "displayPrice",
            "formattedPrice",
            "formattedPlanPrice",
            "planPriceDisplay",
        ):
            decoded = decode_netflix_value(plan_obj.get(key))
            if decoded:
                return decoded
        price_obj = plan_obj.get("price")
        if isinstance(price_obj, dict):
            for key in (
                "displayValue",
                "formatted",
                "formattedPrice",
                "displayPrice",
                "value",
                "amountDisplay",
            ):
                decoded = decode_netflix_value(price_obj.get(key))
                if decoded:
                    return decoded
        return None

    hold_status = _first_bool(
        hold_meta.get("isUserOnHold") if isinstance(hold_meta, dict) else hold_meta,
        hold_meta.get("holdStatus") if isinstance(hold_meta, dict) else None,
        hold_meta.get("isOnHold") if isinstance(hold_meta, dict) else None,
        hold_meta.get("pastDue") if isinstance(hold_meta, dict) else None,
        growth_account.get("isUserOnHold"),
        growth_account.get("holdStatus"),
        growth_account.get("isOnHold"),
        growth_account.get("pastDue"),
        growth_account.get("isPastDue"),
    )

    info = {
        "accountOwnerName": decode_netflix_value(current_profile.get("name")),
        "email": decode_netflix_value(email_value),
        "countryOfSignup": decode_netflix_value(
            ((growth_account.get("countryOfSignUp") or {}).get("code"))
        ),
        "memberSince": decode_netflix_value(growth_account.get("memberSince")),
        "nextBillingDate": decode_netflix_value(
            next_billing.get("localDate") or next_billing.get("date")
        ),
        "userGuid": decode_netflix_value(
            growth_account.get("ownerGuid") or current_profile.get("guid")
        ),
        "showExtraMemberSection": (
            "Yes" if "EXTRA_MEMBER" in feature_types else ("No" if feature_types else None)
        ),
        "membershipStatus": decode_netflix_value(growth_account.get("membershipStatus")),
        "localizedPlanName": decode_netflix_value(
            current_plan.get("name") or next_plan.get("name")
        ),
        "planPrice": _price(current_plan) or _price(next_plan),
        "paymentMethodType": decode_netflix_value(
            payment_logo or growth_account.get("payer")
        ),
        "maskedCard": None,
        "phoneNumber": normalize_phone_number(phone_digits, phone_country),
        "videoQuality": decode_netflix_value(current_plan.get("videoQuality")),
        "maxStreams": decode_netflix_value(current_plan.get("maxStreams")),
        "holdStatus": hold_status,
        "emailVerified": format_boolean_label(email_verified),
        "phoneVerified": format_boolean_label(phone_verified),
        "profiles": ", ".join(profile_names) if profile_names else None,
    }

    if "Card" in payment_typename:
        info["paymentMethodType"] = "CC"
        if payment_display_text:
            info["maskedCard"] = payment_display_text
    elif payment_display_text and payment_logo is None and not re.fullmatch(
        r"\d{4}", payment_display_text or ""
    ):
        info["paymentMethodType"] = info["paymentMethodType"] or payment_display_text

    return {k: v for k, v in info.items() if v not in (None, "", [], {})}


def extract_info(response_text: str) -> Dict[str, Any]:
    graphql_info = extract_info_from_graphql_payload(response_text)

    extra_member_patterns = (
        r"extra\s+member",
        r"miembro\s+extra",
        r"assinante\s+extra",
        r"suscriptor\s+extra",
        r"extra\s+on\s+someone.?else.?s\s+plan",
        r"th[aà]nh\s+vi[eê]n\s+b[oô]sung",
    )
    is_extra = any(
        re.search(p, response_text, re.IGNORECASE) for p in extra_member_patterns
    )

    if has_complete_account_info(graphql_info):
        extracted = dict(graphql_info)
    else:
        extracted = {
            "accountOwnerName": extract_first_match(
                response_text,
                [
                    r'userInfo"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"',
                    r'"accountOwnerName"\s*:\s*"([^"]+)"',
                    r'"name"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
                    r'"firstName"\s*:\s*"([^"]+)"',
                ],
            ),
            "email": extract_first_match(
                response_text,
                [
                    r'"emailAddress"\s*:\s*"([^"]+)"',
                    r'"email"\s*:\s*"([^"]+)"',
                    r'"loginId"\s*:\s*"([^"]+)"',
                ],
            ),
            "countryOfSignup": extract_first_match(
                response_text,
                [r'"currentCountry"\s*:\s*"([^"]+)"', r'"countryOfSignup":\s*"([^"]+)"'],
            ),
            "memberSince": extract_first_match(
                response_text, [r'"memberSince":\s*"([^"]+)"']
            ),
            "nextBillingDate": extract_first_match(
                response_text,
                [
                    r'"GrowthNextBillingDate"\s*,\s*"date"\s*:\s*"([^"T]+)T',
                    r'"nextBillingDate"\s*:\s*"([^"]+)"',
                    r'"nextBilling"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
                ],
            ),
            "userGuid": extract_first_match(
                response_text, [r'"userGuid":\s*"([^"]+)"']
            ),
            "showExtraMemberSection": extract_bool_value(
                response_text,
                [
                    r'"showExtraMemberSection":\s*\{\s*"fieldType":\s*"Boolean",\s*"value":\s*(true|false)',
                    r'"showExtraMemberSection"\s*:\s*(true|false)',
                ],
            ),
            "membershipStatus": extract_first_match(
                response_text, [r'"membershipStatus":\s*"([^"]+)"']
            ),
            "maxStreams": extract_first_match(
                response_text,
                [
                    r'maxStreams\":\{\"fieldType\":\"Numeric\",\"value\":([^,]+),',
                    r'"maxStreams"\s*:\s*"?([^",}]+)"?',
                ],
            ),
            "localizedPlanName": extract_first_match(
                response_text,
                [
                    r'"MemberPlan"\s*,\s*"fields"\s*:\s*\{\s*"localizedPlanName"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
                    r'localizedPlanName\":\{\"fieldType\":\"String\",\"value\":\"([^"]+)"',
                    r'"currentPlan"\s*:\s*\{[\s\S]*?"plan"\s*:\s*\{[\s\S]*?"name"\s*:\s*"([^"]+)"',
                    r'"localizedPlanName"\s*:\s*"([^"]+)"',
                    r'"planName"\s*:\s*"([^"]+)"',
                ],
            ),
            "planPrice": extract_first_match(
                response_text,
                [
                    r'"formattedPlanPrice"\s*:\s*"([^"]+)"',
                    r'"formattedPrice"\s*:\s*"([^"]+)"',
                    r'"planPriceDisplay"\s*:\s*"([^"]+)"',
                    r'"displayPrice"\s*:\s*"([^"]+)"',
                    r'"planPrice"\s*:\s*"([^"]+)"',
                ],
            ),
            "paymentMethodExists": extract_bool_value(
                response_text,
                [
                    r'"paymentMethodExists":\s*\{\s*"fieldType":\s*"Boolean",\s*"value":\s*(true|false)',
                    r'"paymentMethodExists"\s*:\s*(true|false)',
                ],
            ),
            "paymentMethodType": extract_first_match(
                response_text,
                [
                    r'"paymentMethod"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
                    r'"paymentMethod"\s*:\s*"([^"]+)"',
                    r'"paymentType"\s*:\s*"([^"]+)"',
                    r'"paymentMethodType"\s*:\s*"([^"]+)"',
                ],
            ),
            "maskedCard": extract_first_match(
                response_text,
                [
                    r'"__typename"\s*:\s*"GrowthCardPaymentMethod"[\s\S]*?"displayText"\s*:\s*"([^"]+)"',
                    r'"paymentCardDisplayString"\s*:\s*"([^"]+)"',
                    r'"paymentMethodLast4"\s*:\s*"([^"]+)"',
                    r'"lastFour"\s*:\s*"([^"]+)"',
                    r'"maskedCard"\s*:\s*"([^"]+)"',
                ],
            ),
            "phoneNumber": extract_first_match(
                response_text,
                [
                    r'"phoneNumberDigits"\s*:\s*\{[\s\S]*?"value"\s*:\s*"([^"]+)"',
                    r'"phoneNumber"\s*:\s*"([^"]+)"',
                    r'"mobilePhone"\s*:\s*"([^"]+)"',
                ],
            ),
            "phoneVerified": extract_bool_value(
                response_text,
                [r'"phoneVerified"\s*:\s*(true|false)', r'"isPhoneVerified"\s*:\s*(true|false)'],
            ),
            "videoQuality": extract_first_match(
                response_text,
                [
                    r'videoQuality"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
                    r'"videoQuality"\s*:\s*"([^"]+)"',
                    r'"quality"\s*:\s*"([^"]+)"',
                ],
            ),
            "holdStatus": extract_bool_value(
                response_text,
                [
                    r'"holdStatus"\s*:\s*(true|false)',
                    r'"isUserOnHold"\s*:\s*(true|false)',
                    r'"isOnHold"\s*:\s*(true|false)',
                    r'"pastDue"\s*:\s*(true|false)',
                    r'"isPastDue"\s*:\s*(true|false)',
                ],
            ),
            "emailVerified": extract_bool_value(
                response_text,
                [
                    r'"emailVerified"\s*:\s*(true|false)',
                    r'"isEmailVerified"\s*:\s*(true|false)',
                    r'"emailAddressVerified"\s*:\s*(true|false)',
                ],
            ),
            "profiles": extract_profile_names(response_text),
        }
        extracted = merge_info(graphql_info, extracted)

    for key in (
        "paymentMethodType",
        "paymentMethodExists",
        "maskedCard",
        "holdStatus",
        "emailVerified",
        "phoneNumber",
        "countryOfSignup",
        "membershipStatus",
        "localizedPlanName",
    ):
        extracted.setdefault(key, None)

    if is_extra:
        extracted["isExtraMemberAccount"] = "Yes"

    if extracted.get("localizedPlanName"):
        extracted["localizedPlanName"] = extracted["localizedPlanName"].replace(
            "miembro u00A0extra", "(Extra Member)"
        )

    if not extracted.get("paymentMethodType"):
        extracted["paymentMethodType"] = extracted.get("paymentMethodExists")

    if extracted.get("maskedCard") and re.fullmatch(r"\d{4}", str(extracted["maskedCard"])):
        if extracted.get("paymentMethodType") in {None, "", "Yes"}:
            extracted["paymentMethodType"] = "CC"

    if extracted.get("holdStatus") is None:
        status_key = normalize_plan_key(extracted.get("membershipStatus"))
        if status_key == "current_member":
            extracted["holdStatus"] = "No"
        elif any(
            t in status_key
            for t in ("hold", "past_due", "payment_retry", "paused", "suspend")
        ):
            extracted["holdStatus"] = "Yes"

    if extracted.get("emailVerified") is None and extracted.get("email"):
        extracted["emailVerified"] = "Yes"

    extracted["phoneDisplay"] = normalize_phone_number(
        extracted.get("phoneNumber"), extracted.get("countryOfSignup")
    )

    profiles = extracted.get("profiles")
    if profiles:
        extracted["profileCount"] = len([n for n in profiles.split(", ") if n])
        extracted["profilesDisplay"] = profiles
    else:
        extracted["profileCount"] = None
        extracted["profilesDisplay"] = None

    return extracted
