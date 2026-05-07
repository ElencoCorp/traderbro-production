/* =================================
===  OPEN SIDE NAV ==
=================================== */
$(document).ready(function () {
	$('#sidebarCollapse').on('click', function () {
		 $('#nevbarleft').toggleClass('active');
		 $(this).toggleClass('active');
	});
});

jQuery(document).ready(function(){
	jQuery('.dzClickload').click(function(){
		jQuery('.dzClickload').removeClass('active');
		jQuery(this).addClass('active');
	});
	
	jQuery(".content-scroll").mCustomScrollbar({
		setWidth:false,
		setHeight:false,
		axis:"y"
	});	
		
	$(".full-height").css("height", $(window).height());
	
	$("#dz_tree, #dz_tree_rtl").jstree({
		"core": {
			"themes": {
				"responsive": false
			}
		},
		"types": {
			"default": {
				"icon": "fa fa-folder"
			},
			"file": {
				"icon": "fa fa-file-text"
			}
		},
		"plugins": ["types"]
	});
	
	// Add smooth scrolling to all links
	$(".navbar-nav a").on('click', function(event) {
		// Make sure this.hash has a value before overriding default behavior
		if (this.hash !== "") {
			// Prevent default anchor click behavior
			event.preventDefault();

			// Store hash
			var hash = this.hash;

			// Using jQuery's animate() method to add smooth page scroll
			// The optional number (800) specifies the number of milliseconds it takes to scroll to the specified area
			$('html, body').animate({
				scrollTop: $(hash).offset().top
			});
		} // End if
	});
});

$(document).ready(function () {
	'use strict';
	var scrollTop = jQuery(".scroltop");
	/* page scroll top on click function */	
	scrollTop.on('click',function() {
		jQuery("html, body").animate({
			scrollTop: 0
		}, 1000);
		return false;
	})

	jQuery(window).bind("scroll", function() {
		var scroll = jQuery(window).scrollTop();
		if (scroll > 900) {
			jQuery(".scroltop").fadeIn(0);
		} else {
			jQuery(".scroltop").fadeOut(0);
		}
	});
	/* page scroll top on click function end*/
});

/* =================================
===  SMOOTH SCROLL             ====
=================================== */
var scrollAnimationTime = 1200,
    scrollAnimation = 'easeInOutExpo';
$('a.scrollto').bind('click.smoothscroll', function (event) {
    event.preventDefault();
    var target = this.hash;
    $('html, body').stop().animate({
        'scrollTop': $(target).offset().top
    }, scrollAnimationTime, scrollAnimation, function () {
        window.location.hash = target;
    });
});

/* =================================
===  Bootstrap Internet Explorer 10 in Windows 8 and Windows Phone 8 FIX
=================================== */
if (navigator.userAgent.match(/IEMobile\/10\.0/)) {
  var msViewportStyle = document.createElement('style')
  msViewportStyle.appendChild(
    document.createTextNode(
      '@-ms-viewport{width:auto!important}'
    )
  )
  document.querySelector('head').appendChild(msViewportStyle)
}


