from app.privacy import MASK, mask_pii


def test_mask_phone_and_id():
    text, n = mask_pii("家长电话13912345678,身份证32040219900101123X,学号20231234")
    assert n == 2
    assert "13912345678" not in text and "32040219900101123X" not in text
    assert text.count(MASK) == 2
    assert "20231234" in text  # 普通数字不受影响


def test_mask_spaced_phone():
    text, n = mask_pii("电话 139 1234 5678")
    assert n == 1 and MASK in text
