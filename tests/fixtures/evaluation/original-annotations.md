# 第五阶段样例标注：待用户确认

Dataset digest：`c6d4d9edca5e9c80ca45525a48f20c0c36312fc17cb3e92efa4d288f71175f99`

以下均为建议标注，尚未由用户确认。本文件不授权真实模型调用。
同一场景组的所有变体固定在同一集合；留出集不用于调整判定规则。
确认时请指出需修改的 case_id、分类、预期问题、禁止误报项或工具要求。

## empty_input / development

空输入处理与契约变更是一组；包含已知漏报，不能进入留出集。

### empty-guard-removed：删除空列表保护，旧契约仍有效

建议分类：defect；状态：待确认。

输入为空时仍承诺返回 0；删除保护后必达除法。既有真实漏报只关联到本例。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

预期问题 `empty-guard-removed-issue`：

- 触发：mean\(\[\]\)
- 实际／预期：除以 0，抛出 ZeroDivisionError。 / 按未修改的 docstring 返回 0。
- 变更因果：本次删除了空列表提前返回分支。
- 证据：h0001:old:3, h0001:old:4, h0001:new:2, h0001:new:3；置信度：high。


```diff
diff --git a/stats.py b/stats.py
--- a/stats.py
+++ b/stats.py
@@ -1,5 +1,3 @@
 def mean(values):
     """Return 0 for empty input."""
-    if not values:
-        return 0
     return sum(values) / len(values)
```

### empty-contract-changed：同步明确空列表抛异常

建议分类：non_defect；状态：待确认。

docstring 与实现同步修改；diff 未提供仍需返回 0 的反向契约。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

- 禁止误报：仅因空输入不再返回 0 就报告契约回归。

```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,5 +1,5 @@
 def mean(values):
-    """Return 0 for empty input."""
+    """Raise ValueError for empty input."""
     if not values:
-        return 0
+        raise ValueError("empty input")
     return sum(values) / len(values)
```

## index_boundary / development

索引等于长度的回归与修复是一组。

### boundary-check-weakened：边界判断漏掉等于长度

建议分类：defect；状态：待确认。

index 等于长度时也是越界，契约仍要求返回 None。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

预期问题 `boundary-check-weakened-issue`：

- 触发：item\_at\(\[1\], 1\)
- 实际／预期：访问 items\[1\] 抛出 IndexError。 / 返回 None。
- 变更因果：把 &gt;= 改为 &gt;，使等于长度的索引通过保护。
- 证据：h0001:new:2, h0001:new:3, h0001:new:5；置信度：high。


```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,5 +1,5 @@
 def item_at(items, index):
     """For nonnegative index, return None when out of range."""
-    if index >= len(items):
+    if index > len(items):
         return None
     return items[index]
```

### boundary-check-corrected：修复等于长度的已有错误

建议分类：non_defect；状态：待确认。

该变更修复旧的边界缺陷，不是引入新的越界。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

- 禁止误报：把旧版本的越界缺陷报告为本次引入。

```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,5 +1,5 @@
 def item_at(items, index):
     """For nonnegative index, return None when out of range."""
-    if index > len(items):
+    if index >= len(items):
         return None
     return items[index]
```

## exception_contract / development

异常是否违反显式契约，包括有意异常测试。

### parse-error-unhandled：移除契约要求的异常处理

建议分类：defect；状态：待确认。

非法整数字符串的回退仍是明确契约，但异常捕获被删除。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

预期问题 `parse-error-unhandled-issue`：

- 触发：parse\_count\(&quot;abc&quot;\)
- 实际／预期：int\(&quot;abc&quot;\) 抛出 ValueError。 / 返回 0。
- 变更因果：移除了捕获 ValueError 并返回 0 的分支。
- 证据：h0001:new:2, h0001:new:3, h0001:old:5；置信度：high。


```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,6 +1,3 @@
 def parse_count(text):
     """Return 0 when text is not a valid integer string."""
-    try:
-        return int(text)
-    except ValueError:
-        return 0
+    return int(text)
```

### intentional-exception-test：测试有意捕获除零异常

建议分类：non_defect；状态：待确认。

异常发生在 pytest.raises 中，是被验证的预期行为。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

- 禁止误报：把被 pytest.raises 捕获的预期除零异常报告为缺陷。

```diff
diff --git a/test_errors.py b/test_errors.py
new file mode 100644
--- /dev/null
+++ b/test_errors.py
@@ -0,0 +1,5 @@
+import pytest
+
+def test_zero_division():
+    with pytest.raises(ZeroDivisionError):
+        1 / 0
```

## context_evidence / development

区分可取得的契约和 diff 外不可取得的依据。

### caller-contract-context：调用方非空约束位于首轮省略的上下文

建议分类：non_defect；状态：待确认。

同一 diff 的非空输入契约排除了空输入除零；需取回首轮省略的第一行。

工具要求：required。第一行契约在首轮预览外，但可从同单元安全 diff 获得。
工具目标证据：h0001:new:1。

- 禁止误报：忽略非空输入契约，直接断言合法调用会除零。

```diff
diff --git a/context.py b/context.py
--- a/context.py
+++ b/context.py
@@ -1,8 +1,8 @@
 # callers guarantee a nonempty sequence
 # omitted context must be fetched explicitly
 def avg(values):
     total = sum(values)
-    count = max(1, len(values))
+    count = len(values)
     return total / count
 # trailer
 # context outside the first preview
```

### performance-context-missing：双层循环缺少规模与性能目标

建议分类：insufficient_evidence；状态：待确认。

可观察到二次复杂度，但没有规模、SLO 或预期算法依据，无法确定实际性能缺陷。

工具要求：unavailable。所需调用规模和性能约束不在 diff 内，现有工具无法读取仓库外部上下文。
工具目标证据：无。

- 禁止误报：仅凭双层循环给出高置信度性能缺陷。

```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -0,0 +1,6 @@
+def count_pairs(values):
+    total = 0
+    for left in values:
+        for right in values:
+            total += left == right
+    return total
```

## nullable_record / holdout

None 输入契约的移除与等价改写。

### null-guard-removed：移除 None 输入保护

建议分类：defect；状态：待确认。

未修改契约要求 None 返回 unknown；移除保护导致对 None 下标访问。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

预期问题 `null-guard-removed-issue`：

- 触发：display\_name\(None\)
- 实际／预期：对 None 下标访问导致 TypeError。 / 返回 unknown 字符串。
- 变更因果：删除 record is None 的提前返回。
- 证据：h0001:new:2, h0001:new:3, h0001:old:3；置信度：high。


```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,5 +1,3 @@
 def display_name(record):
     """Return unknown when record is None."""
-    if record is None:
-        return "unknown"
     return record["name"]
```

### null-guard-equivalent：None 保护的等价表达式

建议分类：non_defect；状态：待确认。

条件表达式仅计算选中的分支，None 仍返回 unknown。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

- 禁止误报：声称条件表达式会无条件求值 record\[&quot;name&quot;\]。

```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,5 +1,3 @@
 def display_name(record):
     """Return unknown when record is None."""
-    if record is None:
-        return "unknown"
-    return record["name"]
+    return "unknown" if record is None else record["name"]
```

## return_contract / holdout

返回值契约的破坏与等价改写。

### return-statement-removed：删除返回语句导致隐式 None

建议分类：defect；状态：待确认。

构造字典却没有返回，违反未修改的返回契约。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

预期问题 `return-statement-removed-issue`：

- 触发：response\(3\)
- 实际／预期：函数隐式返回 None。 / 返回 \{&quot;value&quot;: 3\}。
- 变更因果：删除最后的 return payload。
- 证据：h0001:new:2, h0001:old:4；置信度：high。


```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,4 +1,3 @@
 def response(value):
     """Return a dict with the supplied value."""
     payload = {"value": value}
-    return payload
```

### return-inline-equivalent：内联返回字典保持契约

建议分类：non_defect；状态：待确认。

删除局部变量但仍显式返回相同字典。

工具要求：not_needed。首轮 diff 已包含判断所需的变更和契约。
工具目标证据：无。

- 禁止误报：把局部变量删除错误解释为返回值丢失。

```diff
diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,4 +1,3 @@
 def response(value):
     """Return a dict with the supplied value."""
-    payload = {"value": value}
-    return payload
+    return {"value": value}
```
