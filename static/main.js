$(document).ready(function () {
	var logged = false;
	var template = null;
	var vars = null;

	var render = function () {
		$('#content').html(Mustache.to_html(template, vars));
	};

	var refreshWidget = $('#refresh_widget');

	var updateVars = function () {
		$.get('/json', function (data) {
			lastRefreshed = new Date();
			if (!logged) {
				console.info(data);
				logged = true;
			}
			vars = data;
			if (template) {
				render();
			}
			refreshWidget.text((new Date()).toLocaleString());
		});
	};

	/* update template */
	$.get('static/template.html', function (data) {
		template = data;
		if (vars) {
			render();
		}
	});

	updateVars();
	setInterval(updateVars, 10000);

	$('#refresh_page').click(function (e) {
		e.preventDefault();
		updateVars();
	});

});
