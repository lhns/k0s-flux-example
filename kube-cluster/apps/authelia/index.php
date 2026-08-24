<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>User Info</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 2rem;
            background-color: #f8f9fa;
        }
        .container {
            max-width: 800px;
            padding: 1.5em;
            margin: auto;
            background: white;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            border-radius: 8px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            border-top: 1px solid #ddd;
        }
        th, td {
            text-align: left;
            padding: 12px 15px;
            border-bottom: 1px solid #ddd;
        }
        th {
            background-color: #f1f1f1;
            width: 30%;
        }
        ul {
            margin: 0;
            padding-left: 20px;
        }

        .badge-wrapper {
            position: relative;
            display: inline-block;
            margin: 0.2em 0.4em 0.2em 0;
        }
        .badge {
            display: inline-flex;
            border-radius: 0.5em;
            font-size: 0.9em;
            font-weight: bold;
            overflow: hidden;
            box-shadow: 0 1px 2px rgba(0,0,0,0.1);
            cursor: default;
        }
        .badge-label {
            background-color: #2c3e50;
            color: white;
            padding: 0.3em 0.6em;
        }
        .badge-value {
            padding: 0.3em 0.6em;
            color: white;
        }
        .badge-yes .badge-value {
            background-color: #28a745;
        }
        .badge-no .badge-value {
            background-color: #d73a49;
        }
        .tooltip {
            visibility: hidden;
            opacity: 0;
            position: absolute;
            background-color: #333;
            color: #fff;
            padding: 5px 8px;
            border-radius: 5px;
            font-size: 0.9em;
            z-index: 1000;
            transition: opacity 0.3s;
            white-space: nowrap;
            top: 2.2em;
            left: 50%;
            transform: translateX(-50%);
        }
        .badge-wrapper:hover .tooltip {
            visibility: visible;
            opacity: 1;
        }
    </style>
</head>
<body>

<div class="container">
    <h2>User Information</h2>
    <table>
        <tr>
            <th>Username</th>
            <td><?= htmlspecialchars($_SERVER['HTTP_REMOTE_USER'] ?? 'N/A') ?></td>
        </tr>
        <tr>
            <th>Email</th>
            <td><?= htmlspecialchars($_SERVER['HTTP_REMOTE_EMAIL'] ?? 'N/A') ?></td>
        </tr>
        <tr>
            <th>Name</th>
            <td><?= htmlspecialchars($_SERVER['HTTP_REMOTE_NAME'] ?? 'N/A') ?></td>
        </tr>
        <tr>
            <th>Roles</th>
            <td>
                <ul>
                    <?php
                    $groups = array_filter(array_map('trim', explode(',', $_SERVER['HTTP_REMOTE_GROUPS'] ?? '')));
                    foreach ($groups as $group) {
                        echo '<li>' . htmlspecialchars($group) . '</li>';
                    }
                    ?>
                </ul>
            </td>
        </tr>
        <tr>
            <th>Access</th>
            <td>
                <div style="display: flex; flex-wrap: wrap;">
                    <?php
                    $accessMatrixJson = getenv('ACCESS_MATRIX') ?: '{}';
                    $accessMatrix = json_decode($accessMatrixJson, true);
                    if (!is_array($accessMatrix)) {
                        $accessMatrix = [];
                    }

                    foreach ($accessMatrix as $service => $requiredGroups) {
                        if (!is_array($requiredGroups)) {
                            $requiredGroups = [];
                        }
                        $hasAccess = count(array_intersect($requiredGroups, $groups)) > 0 || count($requiredGroups) === 0;
                        $badgeClass = $hasAccess ? 'badge-yes' : 'badge-no';
                        $badgeText = $hasAccess ? 'yes' : 'no';

                        echo '<div class="badge-wrapper">';
                        echo "<span class=\"badge $badgeClass\">";
                        echo "<span class=\"badge-label\">" . htmlspecialchars($service) . "</span>";
                        echo "<span class=\"badge-value\">" . htmlspecialchars($badgeText) . "</span>";
                        echo "</span>";
                        echo "<div class=\"tooltip\">";
                        if (count($requiredGroups)) {
                            echo "Required Groups:<ul>";
                            foreach ($requiredGroups as $group) {
                                echo "<li>" . htmlspecialchars($group) . "</li>";
                            }
                            echo "</ul>";
                        } else {
                            echo "Accessible to all users";
                        }
                        echo "</div>";
                        echo '</div>';
                    }
                    ?>
                </div>
            </td>
        </tr>
    </table>
</div>

</body>
</html>
